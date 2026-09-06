"""Persistent asynchronous SiteScanRun service with optional visible L2 handoff."""

from __future__ import annotations

import json
import hashlib
import inspect
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.models import (
    AIAssistantDecisionReceipt,
    BrowserSession,
    ProductionJob,
    ProductionJobEvent,
    SiteCategory,
    SiteCategorySnapshot,
    SiteEntryURL,
    SiteProfile,
    SiteRegistryRecord,
    SiteScanRun,
    SiteTaxonomySnapshot,
    utc_now,
)
from app.services.site_profile import build_site_profile, profile_capability_evidence_valid, profile_capability_status, validate_site_profile


class SiteScanTimeoutError(TimeoutError):
    """Raised when a native site analyzer exceeds the Website-owned budget."""

    def __init__(self, phase: str, timeout_seconds: float) -> None:
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        super().__init__(f"site scan {phase} exceeded {timeout_seconds:g}s")


class SiteScanRuntimeService:
    MAX_HTTP_SCANS = 2
    _browser_lock = threading.Lock()

    def __init__(self, database, output_root: Path, analyzer) -> None:
        self.database = database
        self.output_root = Path(output_root).resolve()
        self.analyzer = analyzer
        self.executor = ThreadPoolExecutor(max_workers=self.MAX_HTTP_SCANS, thread_name_prefix="site-scan")
        self._scheduled: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _analyzer_timeout_seconds() -> float:
        """Return a bounded timeout for one native analyzer phase.

        The analyzer may use a network client or a headed browser whose own
        retry budget is not visible to this service.  A service-level budget
        keeps one inaccessible retailer from occupying the persistent worker
        forever.  The worker thread is daemonized on timeout; any late result
        is deliberately discarded rather than persisted as a false success.
        """

        try:
            raw = float(os.getenv("WEBSITE_SITE_SCAN_TIMEOUT_SECONDS", "120"))
        except (TypeError, ValueError):
            raw = 120.0
        return max(10.0, min(raw, 900.0))

    def _run_analyzer_bounded(self, phase: str, callable_, *args, **kwargs):
        timeout_seconds = self._analyzer_timeout_seconds()
        result: list[Any] = []
        error: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(callable_(*args, **kwargs))
            except BaseException as exc:  # re-raise on the Website worker thread
                error.append(exc)

        worker = threading.Thread(target=invoke, name=f"site-scan-{phase}", daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            raise SiteScanTimeoutError(phase, timeout_seconds)
        if error:
            raise error[0]
        return result[0] if result else None

    def start(self, *, site_key: str, source_url: str, job_id: str | None, live: bool) -> dict[str, Any]:
        session = self.database.session_factory()
        existing_profile_payload: dict[str, Any] | None = None
        try:
            scan_id = f"scan_{uuid4().hex}"
            browser = BrowserSession(
                browser_session_id=f"browser_{uuid4().hex}",
                site_key=site_key,
                job_id=job_id,
                scan_id=scan_id,
                user_data_dir=str(self.output_root / "_system" / "browser_sessions" / site_key / scan_id),
                current_url=source_url,
                status="READY",
            )
            scan = SiteScanRun(
                scan_id=scan_id,
                site_key=site_key,
                source_url=source_url,
                status="QUEUED",
                live=live,
                taxonomy_level="L0",
                brain_status="NOT_NEEDED",
                provider_posts=0,
                job_id=job_id,
                browser_session_id=browser.browser_session_id,
                started_at=utc_now(),
                heartbeat_at=utc_now(),
            )
            session.add(browser)
            session.add(scan)
            if job_id:
                job = session.get(ProductionJob, job_id)
                if job:
                    job.status = "SITE_SCAN_QUEUED"
                    job.current_stage = "SITE_SCAN"
                    job.last_reason = "站点扫描已进入持久化队列，可离开当前页面"
                    self._job_event(session, job, "SITE_SCAN_QUEUED", "QUEUED", job.last_reason, {"scan_id": scan_id})
            session.commit()
        finally:
            session.close()
        self._schedule(scan_id)
        return self.status(scan_id) or {"scan_id": scan_id, "status": "QUEUED"}

    def _schedule(self, scan_id: str) -> None:
        with self._lock:
            if scan_id in self._scheduled:
                return
            self._scheduled.add(scan_id)
        self.executor.submit(self._execute_guarded, scan_id)

    def reconcile_all(self) -> None:
        session = self.database.session_factory()
        try:
            ids = [row.scan_id for row in session.scalars(select(SiteScanRun).where(SiteScanRun.status.in_({"QUEUED", "ANALYZING", "L2_BROWSER"})))]
        finally:
            session.close()
        for scan_id in ids:
            self._schedule(scan_id)
        self._auto_resume_partial()

    # PARTIAL 结果超过该阈值后自动用同一浏览器会话续扫一次（最多 1 次）。
    AUTO_RESUME_PARTIAL_AFTER_SECONDS = 60.0

    def _auto_resume_partial(self) -> None:
        """PARTIAL 扫描自动续扫：减少 sixpenny / mackenzie-childs 等需要
        手动点击"恢复站点浏览器会话"的场景。仅对 resume_count<=1（即尚未
        自动续扫过）的 PARTIAL 结果触发一次。"""
        now = datetime.now(UTC)
        session = self.database.session_factory()
        try:
            candidates = list(session.scalars(select(SiteScanRun).where(
                SiteScanRun.status == "PARTIAL",
                SiteScanRun.resume_count <= 1,
                SiteScanRun.finished_at.isnot(None),
            )))
        finally:
            session.close()
        for scan in candidates:
            if scan.finished_at is None:
                continue
            try:
                age = (now - scan.finished_at).total_seconds()
            except TypeError:
                continue
            if age < self.AUTO_RESUME_PARTIAL_AFTER_SECONDS:
                continue
            session = self.database.session_factory()
            try:
                row = session.get(SiteScanRun, scan.scan_id)
                if row is None or row.status != "PARTIAL" or row.resume_count > 1:
                    continue
                row.status = "QUEUED"
                row.error_code = row.error_message = None
                row.finished_at = None
                row.heartbeat_at = utc_now()
                if row.job_id:
                    job = session.get(ProductionJob, row.job_id)
                    if job:
                        job.last_reason = "站点扫描为 PARTIAL，已自动用同一浏览器会话续扫一次"
                        self._job_event(session, job, "SITE_SCAN_AUTO_RESUME", "QUEUED", job.last_reason, {"scan_id": row.scan_id, "reason_code": "PARTIAL"})
                session.commit()
            finally:
                session.close()
            self._schedule(scan.scan_id)

    def _execute_guarded(self, scan_id: str) -> None:
        try:
            self._execute(scan_id)
        except Exception as error:
            session = self.database.session_factory()
            try:
                scan = session.get(SiteScanRun, scan_id)
                if scan:
                    timed_out = isinstance(error, SiteScanTimeoutError)
                    scan.status = "TEMPORARY_FAILURE" if timed_out else "FAILED"
                    scan.error_code = "SITE_SCAN_TIMEOUT" if timed_out else type(error).__name__.upper()
                    scan.error_message = str(error)[:1000]
                    scan.finished_at = utc_now()
                    scan.heartbeat_at = utc_now()
                    browser = session.get(BrowserSession, scan.browser_session_id) if scan.browser_session_id else None
                    if browser:
                        browser.status = scan.status
                        browser.challenge_code = scan.error_code
                        browser.challenge_message = scan.error_message
                    if scan.job_id:
                        job = session.get(ProductionJob, scan.job_id)
                        if job:
                            job.status = "WAITING_REVIEW"
                            job.current_stage = "SITE_SCAN"
                            job.last_reason = "站点扫描超时，已保留同一会话和检查点，可恢复重试" if timed_out else f"站点扫描失败：{type(error).__name__}"
                            self._job_event(session, job, "SITE_SCAN_TIMEOUT" if timed_out else "SITE_SCAN_FAILED", scan.status, job.last_reason, {"scan_id": scan_id, "reason_code": scan.error_code})
                    session.commit()
            finally:
                session.close()
        finally:
            with self._lock:
                self._scheduled.discard(scan_id)

    @staticmethod
    def _needs_browser_enrichment(receipt: dict[str, Any]) -> bool:
        """Decide whether an L1 result still needs the visible L2 pass.

        A normal HTTP scan can discover a real taxonomy from sitemap/navigation
        evidence while still being unable to read category totals.  Treating
        that result as a final PARTIAL state strands the user with UNKNOWN
        counts and never gives the persistent browser session a chance to
        enrich them.  Escalate only when the result is incomplete; a PARTIAL
        result whose categories already have authoritative counts does not
        incur an unnecessary browser launch.
        """

        status = str(receipt.get("status") or "")
        if status in {"BROWSER_REQUIRED", "HUMAN_REQUIRED"}:
            return True
        if status != "PARTIAL":
            return False
        raw_categories = receipt.get("categories")
        categories = raw_categories if isinstance(raw_categories, list) else []
        return not categories or any(
            isinstance(item, dict) and str(item.get("count_kind") or "UNKNOWN") == "UNKNOWN"
            for item in categories
        )

    def _execute(self, scan_id: str) -> None:
        session = self.database.session_factory()
        # This value is read after the DB session closes.  Initialize it in
        # the worker scope so a first scan, null profile, malformed JSON or a
        # non-object profile all follow the same explicit ``None`` path.
        existing_profile_payload: dict[str, Any] | None = None
        try:
            scan = session.get(SiteScanRun, scan_id)
            if scan is None or scan.status in {"READY", "PARTIAL", "FAILED", "HUMAN_REQUIRED", "ROBOTS_DENIED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "BRAIN_NOT_CONFIGURED", "BROWSER_RUNTIME_NOT_INSTALLED"}:
                return
            scan.status = "ANALYZING"
            scan.heartbeat_at = utc_now()
            scan.resume_count += 1
            source_url, site_key, live = scan.source_url, scan.site_key, scan.live
            browser = session.get(BrowserSession, scan.browser_session_id) if scan.browser_session_id else None
            profile_row = session.get(SiteProfile, site_key)
            if profile_row is not None and profile_row.profile_json:
                try:
                    raw_profile = json.loads(profile_row.profile_json)
                    if isinstance(raw_profile, dict):
                        existing_profile_payload = raw_profile
                except (TypeError, ValueError):
                    existing_profile_payload = None
            session.commit()
        finally:
            session.close()
        output_dir = self.output_root / "_control" / "site_analysis" / site_key / scan_id
        try:
            analyze_parameters = inspect.signature(self.analyzer.analyze).parameters
        except (TypeError, ValueError):
            analyze_parameters = {}
        if "profile" in analyze_parameters:
            receipt = self._run_analyzer_bounded("http", self.analyzer.analyze, source_url, live=live, output_dir=output_dir, profile=existing_profile_payload)
        else:
            receipt = self._run_analyzer_bounded("http", self.analyzer.analyze, source_url, live=live, output_dir=output_dir)
        blocker = receipt.get("blocker") if isinstance(receipt.get("blocker"), dict) else {}
        blocker_code = str(blocker.get("code") or "")
        if self._needs_browser_enrichment(receipt) and browser is not None:
            session = self.database.session_factory()
            try:
                scan = session.get(SiteScanRun, scan_id)
                browser_row = session.get(BrowserSession, browser.browser_session_id)
                if scan:
                    scan.status = "L2_BROWSER"
                    scan.heartbeat_at = utc_now()
                if browser_row:
                    browser_row.status = "RUNNING"
                session.commit()
            finally:
                session.close()
            with self._browser_lock:
                receipt = self._run_analyzer_bounded("browser", self.analyzer.analyze_browser, source_url, output_dir=output_dir, session_dir=Path(browser.user_data_dir))
        self._persist(scan_id, receipt, output_dir)

    def _persist(self, scan_id: str, receipt: dict[str, Any], output_dir: Path) -> None:
        session = self.database.session_factory()
        try:
            scan = session.get(SiteScanRun, scan_id)
            if scan is None:
                return
            site = session.get(SiteRegistryRecord, scan.site_key)
            if site is None:
                return
            snapshot_id = f"tax_{uuid4().hex}"
            raw_categories = receipt.get("categories") if isinstance(receipt.get("categories"), list) else []
            categories: list[tuple[SiteCategory, dict[str, Any]]] = []
            seen_paths: set[str] = set()
            reserved_category_ids: set[str] = set()
            for item in raw_categories:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or item.get("native_name") or "").strip()
                if not path or path in seen_paths:
                    # 同一份 receipt 内可能出现重复 path（导航/促销链接被解析成多条类目），
                    # 数据库有 UNIQUE(site_key, path) 约束，批次内先去重避免批量插入冲突。
                    continue
                seen_paths.add(path)
                category = session.scalar(select(SiteCategory).where(SiteCategory.site_key == site.site_key, SiteCategory.path == path))
                if category is None:
                    requested_category_id = str(item.get("category_id") or "").strip()
                    category_id = requested_category_id or self._site_category_id(site.site_key, path)
                    existing_by_id = session.get(SiteCategory, category_id)
                    if (
                        category_id in reserved_category_ids
                        or existing_by_id is not None
                        and (existing_by_id.site_key != site.site_key or existing_by_id.path != path)
                    ):
                        # Analyzer IDs are often derived from only the URL path.  That is
                        # stable for one site, but SQLite keeps category_id globally unique;
                        # a second retailer with the same /accessories path would otherwise
                        # abort the whole snapshot.  Re-key only the conflicting new row so
                        # existing Job policies and same-site path IDs remain stable.
                        category_id = self._site_category_id(site.site_key, path)
                        scoped = session.get(SiteCategory, category_id)
                        if (
                            category_id in reserved_category_ids
                            or scoped is not None
                            and (scoped.site_key != site.site_key or scoped.path != path)
                        ):
                            category_id = f"cat_{uuid4().hex}"
                    category = SiteCategory(
                        category_id=category_id,
                        site_key=site.site_key,
                        path=path,
                        native_name=str(item.get("native_name") or path),
                        canonical_name=str(item.get("canonical_name") or item.get("native_name") or path),
                    )
                count_value = item.get("count_value") if isinstance(item.get("count_value"), int) else None
                category.snapshot_id = snapshot_id
                category.source_url = str(item.get("source_url") or scan.source_url)
                category.count_value = count_value
                category.count_kind = str(item.get("count_kind") or "UNKNOWN")
                category.reported_count = count_value or 0
                category.discovered_count = 0
                category.eligible_count = 0
                category.confidence = float(item.get("confidence") or 0)
                category.evidence_json = json.dumps(item.get("evidence") or [], ensure_ascii=False)
                category.level = int(item.get("level") or 1)
                category.scope_kind = str(receipt.get("source_scope") or "CATEGORY")
                category.verified_at = utc_now() if receipt.get("verified") else None
                category.last_scanned_at = utc_now()
                session.add(category)
                reserved_category_ids.add(category.category_id)
                categories.append((category, item))
            session.flush()
            by_path = {category.path: category.category_id for category, _ in categories}
            for category, item in categories:
                parent_path = str(item.get("parent_path") or "")
                category.parent_category_id = by_path.get(parent_path) if parent_path else None
            # Keep a snapshot-owned copy before the mutable latest-site rows
            # are refreshed below.  A running/paused Job must continue to use
            # the taxonomy it selected even after a later rescan.
            for category, item in categories:
                session.add(SiteCategorySnapshot(
                    snapshot_id=snapshot_id,
                    category_id=category.category_id,
                    site_key=category.site_key,
                    native_name=category.native_name,
                    canonical_name=category.canonical_name,
                    path=category.path,
                    source_url=category.source_url,
                    count_value=category.count_value,
                    count_kind=category.count_kind,
                    reported_count=category.reported_count,
                    discovered_count=category.discovered_count,
                    eligible_count=category.eligible_count,
                    confidence=category.confidence,
                    evidence_json=category.evidence_json,
                    verified_at=category.verified_at,
                    selected=bool(item.get("selected", category.selected)),
                    last_scanned_at=category.last_scanned_at,
                    parent_category_id=category.parent_category_id,
                    level=category.level,
                    scope_kind=category.scope_kind,
                ))
            snapshot = SiteTaxonomySnapshot(
                snapshot_id=snapshot_id,
                site_key=site.site_key,
                source_url=scan.source_url,
                status=str(receipt.get("status") or "PARTIAL"),
                native_categories_json=json.dumps([item.get("native_name") for _, item in categories], ensure_ascii=False),
                canonical_categories_json=json.dumps([item.get("canonical_name") for _, item in categories], ensure_ascii=False),
                evidence_json=json.dumps(receipt, ensure_ascii=False),
            )
            session.add(snapshot)
            entry = session.scalar(select(SiteEntryURL).where(SiteEntryURL.site_key == site.site_key, SiteEntryURL.url == scan.source_url))
            if entry:
                entry.last_taxonomy_snapshot_id = snapshot_id
                entry.last_status = str(receipt.get("status") or "PARTIAL")
            site.status = "ACTIVE" if receipt.get("verified") else "UNVERIFIED"
            site.source_health = "ACTIVE" if receipt.get("verified") else str(receipt.get("status") or "PARTIAL")
            site.source_kind = str(receipt.get("source_type") or "UNKNOWN")
            site.acquisition_mode = "SCOPE_FIRST" if site.source_kind == "MARKETPLACE" else "CATEGORY_FIRST"
            site.profile_version = str(receipt.get("profile_version") or "native-unverified")
            site.last_verified_at = utc_now() if receipt.get("verified") else site.last_verified_at
            raw_profile = receipt.get("site_profile") if isinstance(receipt.get("site_profile"), dict) else None
            if raw_profile is not None:
                try:
                    profile_contract = validate_site_profile(raw_profile, allow_draft=True)
                    profile_payload = profile_contract.model_dump(mode="json")
                    if str(profile_payload.get("status") or "").upper() == "VALIDATED":
                        capability_status = profile_capability_status(profile_payload)
                        profile_payload.setdefault("evidence", {})
                        if isinstance(profile_payload["evidence"], dict):
                            profile_payload["evidence"]["reusable_capabilities"] = capability_status
                        # Keep a validated profile when at least one capability
                        # has real evidence; downstream consumers select only
                        # the capability they can prove.  A profile with no
                        # verified capability remains DRAFT and is relearned.
                        if not any(capability_status.values()):
                            profile_payload["status"] = "DRAFT"
                            profile_payload["validated_at"] = None
                            profile_payload["last_success_at"] = None
                except (TypeError, ValueError):
                    profile_payload = None
            else:
                profile_payload = None
            if profile_payload is None:
                # Keep the SQL row useful even for older/custom analyzers that
                # return no v1 profile.  This fallback is DRAFT and therefore
                # never considered reusable for an authenticated production run.
                try:
                    profile_payload = build_site_profile(
                        site_key=site.site_key,
                        source_url=scan.source_url,
                        source_type=site.source_kind,
                        platform=str((receipt.get("evidence") or {}).get("l0", {}).get("platform") if isinstance((receipt.get("evidence") or {}).get("l0"), dict) else "UNKNOWN"),
                        evidence={"signals": receipt.get("evidence") or {}, "last_taxonomy_snapshot_id": snapshot_id},
                        status="DRAFT",
                        confidence=1.0 if receipt.get("verified") else 0.0,
                    )
                except (TypeError, ValueError):
                    profile_payload = {"schema_version": "website-site-profile.v1", "site_key": site.site_key, "source_url": scan.source_url, "source_type": site.source_kind, "status": "DRAFT", "last_taxonomy_snapshot_id": snapshot_id}
            profile_payload["last_taxonomy_snapshot_id"] = snapshot_id
            profile_payload["source_url"] = scan.source_url
            profile_payload["site_key"] = site.site_key
            profile = session.get(SiteProfile, site.site_key)
            persisted_profile = True
            existing_profile_payload: dict[str, Any] | None = None
            if profile is not None and profile.profile_json:
                try:
                    parsed_existing = json.loads(profile.profile_json)
                    if isinstance(parsed_existing, dict):
                        existing_profile_payload = parsed_existing
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_profile_payload = None
            incoming_status = str(profile_payload.get("status") or "DRAFT").upper()
            existing_status = str((existing_profile_payload or {}).get("status") or getattr(profile, "status", "DRAFT")).upper()
            # A late/failed scan must never downgrade a reusable profile.  A
            # successful replacement is promoted only if the row still refers
            # to the version this scan observed (optimistic concurrency); this
            # prevents an older worker from relabelling a newer profile STALE.
            if profile is not None and existing_status == "VALIDATED" and incoming_status != "VALIDATED":
                profile_payload = existing_profile_payload or profile_payload
                persisted_profile = False
            elif profile is not None and existing_profile_payload:
                incoming_previous = str(
                    ((receipt.get("brain") or {}).get("site_profile_migrated_from") if isinstance(receipt.get("brain"), dict) else "")
                    or ""
                )
                incoming_profile_version = str(profile_payload.get("profile_version") or "")
                existing_version = str(existing_profile_payload.get("profile_version") or "")
                try:
                    newer_row = bool(profile.updated_at and scan.started_at and profile.updated_at.replace(tzinfo=UTC) > scan.started_at.replace(tzinfo=UTC))
                except (AttributeError, TypeError, ValueError):
                    newer_row = False
                if newer_row and incoming_profile_version != existing_version and incoming_previous != existing_version:
                    profile_payload = existing_profile_payload
                    persisted_profile = False
            if profile is None:
                profile = SiteProfile(site_key=site.site_key, source_url=scan.source_url, profile_json=json.dumps(profile_payload, ensure_ascii=False), rules_version="website-site-profile.v1", status=str(profile_payload.get("status") or "DRAFT"))
                session.add(profile)
            elif persisted_profile:
                profile.profile_json = json.dumps(profile_payload, ensure_ascii=False)
                profile.rules_version = "website-site-profile.v1"
                profile.status = str(profile_payload.get("status") or "DRAFT")
            brain = receipt.get("brain") if isinstance(receipt.get("brain"), dict) else {}
            scan.status = str(receipt.get("status") or "PARTIAL")
            scan.taxonomy_level = str(receipt.get("taxonomy_level") or "L0")
            scan.brain_status = str(brain.get("status") or "NOT_NEEDED")
            scan.provider_posts = int(brain.get("provider_posts") or 0)
            scan.receipt_path = str(output_dir / "taxonomy_receipt.json")
            blocker = receipt.get("blocker") if isinstance(receipt.get("blocker"), dict) else {}
            scan.error_code = str(blocker.get("code") or "") or None
            scan.error_message = str(blocker.get("message") or "") or None
            scan.result_json = json.dumps({
                "snapshot_id": snapshot_id,
                "verified": bool(receipt.get("verified")),
                "category_count": len(categories),
                "site_profile_version": profile_payload.get("profile_version"),
                "site_profile_status": profile_payload.get("status"),
                "site_profile_persisted": persisted_profile,
                "site_profile_reused": bool((receipt.get("brain") or {}).get("site_profile_reused")) if isinstance(receipt.get("brain"), dict) else False,
            }, ensure_ascii=False)
            scan.finished_at = utc_now()
            scan.heartbeat_at = utc_now()
            browser = session.get(BrowserSession, scan.browser_session_id) if scan.browser_session_id else None
            if browser:
                browser.status = scan.status if scan.status in {"HUMAN_REQUIRED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN"} else "READY"
                browser.challenge_code = scan.error_code if scan.status == "HUMAN_REQUIRED" else None
                browser.challenge_message = scan.error_message if scan.status == "HUMAN_REQUIRED" else None
            if categories:
                session.execute(delete(SiteCategory).where(SiteCategory.site_key == site.site_key, SiteCategory.snapshot_id != snapshot_id), execution_options={"synchronize_session": False})
            if scan.job_id:
                job = session.get(ProductionJob, scan.job_id)
                if job:
                    policy = json.loads(job.policy_json or "{}")
                    policy["category_snapshot_id"] = snapshot_id
                    policy["site_scan_id"] = scan_id
                    job.policy_json = json.dumps(policy, ensure_ascii=False)
                    job.status = "TAXONOMY_READY" if receipt.get("verified") else "HUMAN_REQUIRED" if scan.status == "HUMAN_REQUIRED" else "WAITING_REVIEW"
                    job.current_stage = "TAXONOMY_SELECTION"
                    job.last_reason = "站点类目扫描完成" if categories else (scan.error_message or "站点扫描需要处理")
                    self._job_event(session, job, "SITE_SCAN_COMPLETED", job.status, job.last_reason, {"scan_id": scan_id, "snapshot_id": snapshot_id, "verified": bool(receipt.get("verified")), "reason_code": scan.error_code})
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _site_category_id(site_key: str, path: str) -> str:
        """Return a deterministic ID that is unique across sites for a path.

        Receipts produced by older analyzers may use a path-only hash.  The
        persistence layer owns the database boundary, so it must protect the
        global primary key without changing IDs that are already bound to a
        same-site category path.
        """

        digest = hashlib.sha256(f"{site_key}\x00{path}".encode("utf-8")).hexdigest()[:16]
        return f"cat_{digest}"

    @staticmethod
    def _job_event(session, job: ProductionJob, event_type: str, status: str, message: str, payload: dict[str, Any]) -> None:
        sequence = int(session.scalar(select(func.coalesce(func.max(ProductionJobEvent.sequence), 0)).where(ProductionJobEvent.job_id == job.job_id)) or 0) + 1
        session.add(ProductionJobEvent(job_id=job.job_id, sequence=sequence, event_type=event_type, status=status, message=message, stage=job.current_stage, payload_json=json.dumps(payload, ensure_ascii=False)))

    def status(self, scan_id: str) -> dict[str, Any] | None:
        session = self.database.session_factory()
        try:
            scan = session.get(SiteScanRun, scan_id)
            if scan is None:
                return None
            result = json.loads(scan.result_json) if scan.result_json else {}
            return {
                "scan_id": scan.scan_id,
                "site_key": scan.site_key,
                "job_id": scan.job_id,
                "source_url": scan.source_url,
                "status": scan.status,
                "live": scan.live,
                "taxonomy_level": scan.taxonomy_level,
                "brain_status": scan.brain_status,
                "provider_posts": scan.provider_posts,
                "browser_session_id": scan.browser_session_id,
                "error_code": scan.error_code,
                "error_message": scan.error_message,
                "result": result,
                "started_at": scan.started_at.isoformat(),
                "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
            }
        finally:
            session.close()

    def resume(self, scan_id: str) -> dict[str, Any]:
        session = self.database.session_factory()
        try:
            scan = session.get(SiteScanRun, scan_id)
            if scan is None:
                raise KeyError(scan_id)
            if scan.status not in {"HUMAN_REQUIRED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "FAILED", "BROWSER_RUNTIME_NOT_INSTALLED", "PARTIAL", "BRAIN_NOT_CONFIGURED"}:
                return self.status(scan_id) or {}
            scan.status = "QUEUED"
            scan.error_code = scan.error_message = None
            scan.finished_at = None
            scan.heartbeat_at = utc_now()
            session.commit()
        finally:
            session.close()
        self._schedule(scan_id)
        return self.status(scan_id) or {}

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop the scan executor.

        Production callers keep the historical non-blocking behavior, while
        bounded acceptance workers can request ``wait=True`` after a durable
        terminal receipt so no browser/network task survives its evidence
        boundary.
        """

        self.executor.shutdown(wait=wait, cancel_futures=False)


__all__ = ["SiteScanRuntimeService"]
