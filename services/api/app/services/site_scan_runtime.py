"""Persistent asynchronous SiteScanRun service with optional visible L2 handoff."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
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
    SiteEntryURL,
    SiteProfile,
    SiteRegistryRecord,
    SiteScanRun,
    SiteTaxonomySnapshot,
    utc_now,
)


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

    def start(self, *, site_key: str, source_url: str, job_id: str | None, live: bool) -> dict[str, Any]:
        session = self.database.session_factory()
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

    def _execute_guarded(self, scan_id: str) -> None:
        try:
            self._execute(scan_id)
        except Exception as error:
            session = self.database.session_factory()
            try:
                scan = session.get(SiteScanRun, scan_id)
                if scan:
                    scan.status = "FAILED"
                    scan.error_code = type(error).__name__.upper()
                    scan.error_message = str(error)[:1000]
                    scan.finished_at = utc_now()
                    scan.heartbeat_at = utc_now()
                    if scan.job_id:
                        job = session.get(ProductionJob, scan.job_id)
                        if job:
                            job.status = "WAITING_REVIEW"
                            job.current_stage = "SITE_SCAN"
                            job.last_reason = f"站点扫描失败：{type(error).__name__}"
                            self._job_event(session, job, "SITE_SCAN_FAILED", "FAILED", job.last_reason, {"scan_id": scan_id, "reason_code": scan.error_code})
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
        try:
            scan = session.get(SiteScanRun, scan_id)
            if scan is None or scan.status in {"READY", "PARTIAL", "FAILED", "HUMAN_REQUIRED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "BRAIN_NOT_CONFIGURED", "BROWSER_RUNTIME_NOT_INSTALLED"}:
                return
            scan.status = "ANALYZING"
            scan.heartbeat_at = utc_now()
            scan.resume_count += 1
            source_url, site_key, live = scan.source_url, scan.site_key, scan.live
            browser = session.get(BrowserSession, scan.browser_session_id) if scan.browser_session_id else None
            session.commit()
        finally:
            session.close()
        output_dir = self.output_root / "_control" / "site_analysis" / site_key / scan_id
        receipt = self.analyzer.analyze(source_url, live=live, output_dir=output_dir)
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
                receipt = self.analyzer.analyze_browser(source_url, output_dir=output_dir, session_dir=Path(browser.user_data_dir))
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
                    category = SiteCategory(
                        category_id=str(item.get("category_id") or f"cat_{uuid4().hex}"),
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
                categories.append((category, item))
            session.flush()
            by_path = {category.path: category.category_id for category, _ in categories}
            for category, item in categories:
                parent_path = str(item.get("parent_path") or "")
                category.parent_category_id = by_path.get(parent_path) if parent_path else None
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
            profile_payload = {"source_url": scan.source_url, "site_key": site.site_key, "source_type": site.source_kind, "signals": receipt.get("evidence") or {}, "last_taxonomy_snapshot_id": snapshot_id}
            profile = session.get(SiteProfile, site.site_key)
            if profile is None:
                profile = SiteProfile(site_key=site.site_key, source_url=scan.source_url, profile_json=json.dumps(profile_payload, ensure_ascii=False), rules_version="website-site-profile.v2", status="ready" if receipt.get("verified") else "review")
                session.add(profile)
            else:
                profile.profile_json = json.dumps(profile_payload, ensure_ascii=False)
                profile.rules_version = "website-site-profile.v2"
                profile.status = "ready" if receipt.get("verified") else "review"
            brain = receipt.get("brain") if isinstance(receipt.get("brain"), dict) else {}
            scan.status = str(receipt.get("status") or "PARTIAL")
            scan.taxonomy_level = str(receipt.get("taxonomy_level") or "L0")
            scan.brain_status = str(brain.get("status") or "NOT_NEEDED")
            scan.provider_posts = int(brain.get("provider_posts") or 0)
            scan.receipt_path = str(output_dir / "taxonomy_receipt.json")
            blocker = receipt.get("blocker") if isinstance(receipt.get("blocker"), dict) else {}
            scan.error_code = str(blocker.get("code") or "") or None
            scan.error_message = str(blocker.get("message") or "") or None
            scan.result_json = json.dumps({"snapshot_id": snapshot_id, "verified": bool(receipt.get("verified")), "category_count": len(categories)}, ensure_ascii=False)
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

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)


__all__ = ["SiteScanRuntimeService"]
