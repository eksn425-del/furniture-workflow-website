"""Durable Website Native Agentless production runtime.

The Website owns the queue/read-model persistence and launches only the native
Website worker. The frozen Skills package is not imported, spawned, or needed.
The worker emits the same versioned JSONL contract so the existing recovery,
SSE, and receipt gates remain durable across restarts.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    BrowserSession,
    ProductionArtifact,
    ProductionJob,
    ProductionJobEvent,
    ProductionProviderTask,
    ProductionRun,
    ProviderSafetyCheck,
    RuntimeEvent,
    SiteCategory,
    SiteEntryURL,
    SiteRegistryRecord,
    utc_now,
)


ACTIVE_STATUSES = {"QUEUED", "RUNNING", "HUMAN_REQUIRED", "BLOCKED"}
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
TERMINAL_RUNTIME_EVENTS = {
    "JOB_COMPLETED", "JOB_BLOCKED", "JOB_FAILED", "JOB_CANCELLED",
    "HUMAN_REQUIRED", "TARGET_SHORTAGE",
}
# Python cold-start and import time on Windows can be longer than the first
# browser polling interval.  A missing PID during this short launch window is
# not evidence that the worker failed; the append-only event stream remains the
# source of truth and the run is reconciled after this bounded grace period.
RUNTIME_STARTUP_GRACE_SECONDS = 8.0
RUNTIME_EVENT_FLUSH_SECONDS = 2.0
MAX_EMPTY_LAUNCH_RETRIES = 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _utc_age_seconds(value: datetime | None) -> float:
    """Return elapsed seconds for both SQLite-naive and UTC-aware timestamps."""
    if value is None:
        return RUNTIME_STARTUP_GRACE_SECONDS
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return max(0.0, (datetime.now(UTC) - normalized).total_seconds())


def _expected_executable(command_json: str | None) -> str | None:
    try:
        command = json.loads(command_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(command, list) or not command or not isinstance(command[0], str):
        return None
    return command[0]


def _process_alive(pid: int | None, expected_executable: str | None = None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    if not expected_executable:
        return True

    expected_name = Path(expected_executable).name.casefold()
    if os.name == "nt":
        for attempt in range(4):
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            rows = list(csv.reader(line for line in result.stdout.splitlines() if line.strip()))
            if rows and rows[0] and not rows[0][0].startswith("INFO:"):
                return Path(rows[0][0]).name.casefold() == expected_name
            # tasklist can briefly miss a just-created process. Retry only for
            # that bounded window; a process that has really exited must not
            # remain RUNNING forever.
            if attempt < 3:
                time.sleep(0.05)
        return False

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.is_file():
        try:
            first_argument = proc_cmdline.read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            return Path(first_argument).name.casefold() == expected_name
        except OSError:
            return False
    return True


def _terminate_process(pid: int | None, expected_executable: str | None = None) -> None:
    if not pid or not _process_alive(pid, expected_executable):
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def _payload(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


class ProductionRuntimeService:
    MAX_ACTIVE_PRODUCTION_JOBS = 1

    def __init__(self, database, output_root: Path, legacy_runtime: object | None = None) -> None:
        self.database = database
        self.output_root = Path(output_root).resolve()
        self.native_entrypoint = Path(__file__).resolve().parents[4] / "workers" / "native_runtime.py"
        self.python_executable = Path(sys.executable).resolve()

    def _workspace(self, job: ProductionJob) -> Path:
        path = self.output_root / "web_productions" / job.site_key / job.job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _contract(self, session, job: ProductionJob, workspace: Path) -> tuple[Path, Path, dict[str, Any]]:
        contract_path = workspace / "job_contract.json"
        events_path = workspace / "runtime_events.jsonl"
        policy = _payload(job.policy_json)
        requested_ids = [str(value) for value in policy.get("category_ids") or [] if value]
        category_query = select(SiteCategory).where(
            SiteCategory.site_key == job.site_key,
            SiteCategory.category_id.in_(requested_ids),
        ) if requested_ids else select(SiteCategory).where(SiteCategory.site_key == job.site_key).where(False)
        snapshot_id = str(policy.get("category_snapshot_id") or "").strip()
        if snapshot_id:
            category_query = category_query.where(SiteCategory.snapshot_id == snapshot_id)
        category_rows = list(session.scalars(category_query))
        category_by_id = {row.category_id: row for row in category_rows}
        categories = []
        for category_id in requested_ids:
            row = category_by_id.get(category_id)
            if row is None:
                continue
            categories.append({
                "category_id": row.category_id,
                "canonical_name": row.canonical_name,
                "native_name": row.native_name,
                "path": row.path,
                "source_url": row.source_url,
                "parent_category_id": row.parent_category_id,
                "level": row.level,
                "scope_kind": row.scope_kind,
                "count_value": row.count_value,
                "count_kind": row.count_kind,
                "selected": True,
            })
        browser_session = session.scalar(
            select(BrowserSession).where(
                BrowserSession.job_id == job.job_id,
                BrowserSession.site_key == job.site_key,
            ).order_by(BrowserSession.created_at.desc())
        )
        if browser_session is None:
            browser_session = BrowserSession(
                browser_session_id=f"browser_{uuid4().hex}",
                site_key=job.site_key,
                job_id=job.job_id,
                user_data_dir=str(self.output_root / "_system" / "browser_sessions" / job.site_key / job.job_id),
                status="READY",
            )
            session.add(browser_session)
            session.flush()
        site = session.get(SiteRegistryRecord, job.site_key)
        contract = {
            "schema_version": "job-contract.v3",
            "job_id": job.job_id,
            "source_url": job.source_url,
            "site_key": job.site_key,
            "site_display_name": site.display_name if site else job.site_key,
            "title": job.title,
            "goal": job.goal,
            "is_brand_library": bool(job.is_brand_library),
            "brand_name": str(job.brand_name or ""),
            "target_mode": job.target_mode,
            "target_value": job.target_value,
            "category_ids": requested_ids,
            "categories": categories,
            "category_snapshot_id": policy.get("category_snapshot_id"),
            "scope": job.scope,
            "category_allocation": job.category_allocation,
            "allocation_strategy": job.allocation_strategy,
            "spillover": job.spillover,
            "allow_shortfall_delivery": bool(policy.get("allow_shortfall_delivery", False)),
            "source_type": site.source_kind if site else "UNKNOWN",
            "provider": job.provider,
            "authorization": {
                "approve_paid_generation": job.provider.upper() != "OFF" and job.provider_safety == "PRODUCTION_READY",
            },
            "fixture": policy.get("fixture"),
            "test_profile": policy.get("test_profile"),
            "provider_concurrency": policy.get("provider_concurrency"),
            "browser_session": {
                "browser_session_id": browser_session.browser_session_id,
                "user_data_dir": browser_session.user_data_dir,
            },
            "delivery_requested": True,
            "workspace": str(workspace),
            "database_path": str(self.database.path),
            "approved_plan": _payload(job.plan_json),
        }
        contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return contract_path, events_path, contract

    def _run_dict(self, run: ProductionRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        return {
            "run_id": run.run_id,
            "job_id": run.job_id,
            "status": run.status,
            "stage": run.stage,
            "progress_note": run.progress_note,
            "items_done": run.items_done,
            "items_total": run.items_total,
            "exit_code": run.exit_code,
            "workspace": run.workspace,
            "stdout_tail": run.stdout_tail,
            "error": run.error,
            "pid": run.pid,
            "events_path": run.events_path,
            "contract_path": run.contract_path,
            "checkpoint_path": run.checkpoint_path,
            "queue_position": run.queue_position,
            "launch_attempts": run.launch_attempts,
            "heartbeat_at": _iso(run.heartbeat_at),
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
        }

    def _latest_run(self, session, job_id: str) -> ProductionRun | None:
        return session.scalar(select(ProductionRun).where(ProductionRun.job_id == job_id).order_by(ProductionRun.created_at.desc()))

    def _active_run(self, session) -> ProductionRun | None:
        return session.scalar(select(ProductionRun).where(ProductionRun.status == "RUNNING").order_by(ProductionRun.created_at.asc()))

    @staticmethod
    def _provider_ready(session, job: ProductionJob) -> bool:
        if job.provider.upper() == "OFF":
            return True
        check = session.scalar(
            select(ProviderSafetyCheck)
            .where(ProviderSafetyCheck.job_id == job.job_id, ProviderSafetyCheck.provider == job.provider)
            .order_by(ProviderSafetyCheck.created_at.desc())
        )
        return bool(
            check
            and check.status == "PRODUCTION_READY"
            and check.provider_idempotency_safe
            and check.submission_unknown_guard
        )

    def _job_event_sequence(self, session, job_id: str) -> int:
        return int(session.scalar(select(func.coalesce(func.max(ProductionJobEvent.sequence), 0)).where(ProductionJobEvent.job_id == job_id)) or 0) + 1

    @staticmethod
    def _safe_artifact_path(workspace: Path | None, raw_path: str) -> tuple[Path, str] | None:
        if not raw_path:
            return None
        base = (workspace or Path.cwd()).resolve()
        candidate = Path(raw_path).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        try:
            relative = candidate.relative_to(base).as_posix()
        except ValueError:
            return None
        if not relative or relative == ".":
            return None
        return candidate, relative

    def ingest_event(
        self,
        session,
        event: Mapping[str, Any],
        *,
        run_id: str | None = None,
        workspace: Path | None = None,
    ) -> bool:
        job_id = str(event.get("job_id") or "")
        source_event_id = str(event.get("event_id") or "")
        if not job_id or not source_event_id:
            return False
        if session.get(RuntimeEvent, source_event_id) is not None or session.scalar(
            select(ProductionJobEvent.event_id).where(
                ProductionJobEvent.runtime_event_id == source_event_id
            )
        ) is not None:
            return False
        job = session.get(ProductionJob, job_id)
        if job is None:
            return False
        payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), Mapping) else {}
        raw = RuntimeEvent(
            runtime_event_id=source_event_id,
            job_id=job_id,
            sequence=int(event.get("sequence") or 0),
            schema_version=str(event.get("schema_version") or "workflow-event.v2"),
            event_type=str(event.get("type") or "RUNTIME_EVENT"),
            stage=str(event.get("stage") or "") or None,
            status=str(event.get("status") or "RUNNING"),
            message=str(event.get("message") or "")[:2000],
            items_done=int(event["done"]) if event.get("done") is not None else None,
            items_total=int(event["total"]) if event.get("total") is not None else None,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        try:
            # Browser polling can reconcile the same run concurrently.  Keep
            # both read models in one savepoint so a losing writer treats a
            # duplicate event as an idempotent no-op instead of surfacing a
            # 500 from the UNIQUE(runtime_event_id) constraint.
            with session.begin_nested():
                session.add(raw)
                session.add(ProductionJobEvent(
                    job_id=job_id,
                    sequence=self._job_event_sequence(session, job_id),
                    event_type=raw.event_type,
                    runtime_event_id=source_event_id,
                    schema_version=raw.schema_version,
                    source_sequence=raw.sequence,
                    status=raw.status,
                    message=raw.message,
                    stage=raw.stage,
                    items_done=raw.items_done,
                    items_total=raw.items_total,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                ))
                session.flush()
        except IntegrityError as exc:
            # The pre-check above closes the normal path.  This branch is for
            # the small race between two API polls; only suppress the event
            # identity collision, and preserve all other integrity failures.
            if "runtime_event_id" not in str(exc).casefold():
                raise
            return False
        event_type = raw.event_type
        if raw.stage:
            job.current_stage = raw.stage
        job.last_reason = raw.message or job.last_reason
        if raw.items_done is not None:
            job.delivered_count = max(job.delivered_count, raw.items_done if event_type in {"DELIVERY_COMPLETED", "JOB_COMPLETED"} else job.delivered_count)
        for key, attribute in (("reported_count", "reported_count"), ("discovered_count", "discovered_count"), ("unique_count", "unique_count"), ("eligible_count", "eligible_count"), ("delivered", "delivered_count")):
            if isinstance(payload.get(key), int):
                setattr(job, attribute, int(payload[key]))
        ledger_posts = session.scalar(
            select(func.coalesce(func.sum(ProductionProviderTask.post_attempts), 0)).where(
                ProductionProviderTask.job_id == job_id
            )
        )
        event_posts = payload.get("provider_calls") if isinstance(payload.get("provider_calls"), int) else 0
        job.provider_calls = max(job.provider_calls, int(event_posts), int(ledger_posts or 0))
        if isinstance(payload.get("ready_count"), int):
            job.ready_count = int(payload["ready_count"])
        if event_type == "TARGET_SHORTAGE":
            job.status = "TARGET_SHORTAGE"
            job.shortage_count = int(payload.get("shortage") or max(0, (raw.items_total or 0) - (raw.items_done or 0)))
        elif event_type == "HUMAN_REQUIRED":
            job.status = "HUMAN_REQUIRED"
        elif event_type == "READY_POOL_COMPLETED":
            job.status = "READY_POOL"
        elif raw.status in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            job.status = {"SUCCEEDED": "COMPLETED", "FAILED": "FAILED", "CANCELLED": "CANCELLED", "BLOCKED": "BLOCKED"}[raw.status]
        elif raw.status == "RUNNING" and job.status not in {"DRAFT", "WAITING_REVIEW", "POLICY_READY"}:
            job.status = "RUNNING"
        if event_type in {"ARTIFACT_READY", "DELIVERY_COMPLETED"}:
            artifact_type = str(payload.get("artifact_type") or "ZIP")
            safe_path = self._safe_artifact_path(workspace, str(payload.get("relative_path") or ""))
            if safe_path is not None:
                candidate, relative_path = safe_path
                existing = session.scalar(select(ProductionArtifact).where(
                    ProductionArtifact.job_id == job_id,
                    ProductionArtifact.artifact_type == artifact_type,
                    ProductionArtifact.relative_path == relative_path,
                ))
                expected_sha = str(payload.get("sha256") or "") or None
                actual_sha = None
                size_bytes = 0
                if candidate.is_file():
                    import hashlib

                    size_bytes = candidate.stat().st_size
                    actual_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
                status = "DELIVERED" if event_type == "DELIVERY_COMPLETED" else "ARTIFACT_READY"
                if expected_sha and actual_sha and expected_sha.casefold() != actual_sha.casefold():
                    status = "BLOCKED"
                manifest = payload.get("manifest")
                manifest_json = manifest if isinstance(manifest, Mapping) else payload.get("manifest_json")
                if existing is None:
                    existing = ProductionArtifact(
                        artifact_id=f"artifact_{uuid4().hex}",
                        job_id=job_id,
                        run_id=run_id,
                        artifact_type=artifact_type,
                        status=status,
                        relative_path=relative_path,
                        sha256=expected_sha or actual_sha,
                        size_bytes=size_bytes,
                        item_count=int(payload.get("item_count") or payload.get("count") or 0),
                        manifest_schema=str(payload.get("manifest_schema") or "") or None,
                        receipt_json=json.dumps(payload, ensure_ascii=False),
                        manifest_json=json.dumps(manifest_json, ensure_ascii=False) if manifest_json is not None else None,
                    )
                    session.add(existing)
                else:
                    existing.run_id = run_id or existing.run_id
                    existing.status = status if status != "ARTIFACT_READY" or existing.status != "DELIVERED" else existing.status
                    existing.sha256 = expected_sha or actual_sha or existing.sha256
                    existing.size_bytes = size_bytes or existing.size_bytes
                    existing.item_count = int(payload.get("item_count") or payload.get("count") or existing.item_count)
                    existing.manifest_schema = str(payload.get("manifest_schema") or existing.manifest_schema or "") or None
                    existing.receipt_json = json.dumps(payload, ensure_ascii=False)
                    if manifest_json is not None:
                        existing.manifest_json = json.dumps(manifest_json, ensure_ascii=False)
        session.flush()
        return True

    def _ingest_file(self, session, run: ProductionRun) -> int:
        if not run.events_path:
            return 0
        path = Path(run.events_path)
        if not path.is_file():
            return 0
        added = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, Mapping) and self.ingest_event(
                session,
                event,
                run_id=run.run_id,
                workspace=Path(run.workspace) if run.workspace else None,
            ):
                added += 1
        return added

    def _terminal_event_seen(self, session, job_id: str, *, since: datetime | None = None) -> bool:
        event_query = select(RuntimeEvent.runtime_event_id).where(
            RuntimeEvent.job_id == job_id,
            RuntimeEvent.event_type.in_(TERMINAL_RUNTIME_EVENTS),
        )
        if since is not None:
            event_query = event_query.where(RuntimeEvent.created_at >= since)
        return session.scalar(event_query) is not None

    def _runtime_event_seen(self, session, job_id: str, *, since: datetime | None = None) -> bool:
        event_query = select(RuntimeEvent.runtime_event_id).where(RuntimeEvent.job_id == job_id)
        if since is not None:
            event_query = event_query.where(RuntimeEvent.created_at >= since)
        return session.scalar(event_query) is not None

    @staticmethod
    def _command_is_resume(command_json: str | None) -> bool:
        try:
            command = json.loads(command_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(command, list) and "--resume" in command

    def _sync_run_from_events(self, session, run: ProductionRun) -> None:
        event_query = select(RuntimeEvent).where(RuntimeEvent.job_id == run.job_id)
        launch_time = run.claimed_at or run.started_at
        if launch_time is not None:
            # A resumed Job intentionally reuses the append-only event stream.
            # Do not let the previous attempt's terminal event overwrite the
            # current Run's stage while its new worker is starting.
            event_query = event_query.where(RuntimeEvent.created_at >= launch_time)
        latest = session.scalar(event_query.order_by(RuntimeEvent.sequence.desc()))
        if latest is not None:
            run.stage = latest.stage or run.stage
            run.progress_note = latest.message or run.progress_note
            if latest.items_done is not None:
                run.items_done = latest.items_done
            if latest.items_total is not None:
                run.items_total = latest.items_total
        if run.workspace:
            log_path = Path(run.workspace) / "website_runtime_launcher.log"
            if log_path.is_file():
                try:
                    run.stdout_tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
                except OSError:
                    pass

    def reconcile_run(self, run_id: str) -> dict[str, Any] | None:
        session = self.database.session_factory()
        try:
            run = session.get(ProductionRun, run_id)
            if run is None:
                return None
            self._ingest_file(session, run)
            self._sync_run_from_events(session, run)
            run.heartbeat_at = utc_now()
            if run.status == "RUNNING" and not _process_alive(run.pid, _expected_executable(run.command_json)):
                # A short-lived runtime can finish between two API polls while
                # the last JSONL lines are still being appended. Do not turn
                # that partial read into a permanent FAILED run; re-read the
                # append-only stream for a bounded flush window first. A newly
                # launched worker gets a separate startup grace period so each
                # browser poll stays fast instead of sleeping through the full
                # flush window.
                launch_time = run.claimed_at or run.started_at or run.created_at
                launch_age = _utc_age_seconds(launch_time)
                if not self._terminal_event_seen(session, run.job_id, since=launch_time) and launch_age < RUNTIME_STARTUP_GRACE_SECONDS:
                    session.commit()
                    return self._run_dict(run)
                if not self._terminal_event_seen(session, run.job_id, since=launch_time):
                    for _ in range(20):
                        time.sleep(RUNTIME_EVENT_FLUSH_SECONDS / 20)
                        self._ingest_file(session, run)
                        if self._terminal_event_seen(session, run.job_id, since=launch_time):
                            break
                self._sync_run_from_events(session, run)
                job = session.get(ProductionJob, run.job_id)
                if job is not None and self._terminal_event_seen(session, run.job_id, since=launch_time):
                    if job.status == "COMPLETED":
                        run.status, run.stage, run.exit_code = "SUCCEEDED", "DELIVERY", 0
                    elif job.status == "CANCELLED":
                        run.status, run.stage, run.exit_code = "CANCELLED", "CANCELLED", 130
                    elif job.status in {"BLOCKED", "HUMAN_REQUIRED", "TARGET_SHORTAGE"}:
                        run.status, run.stage, run.exit_code = "BLOCKED", job.current_stage, 2
                    else:
                        run.status, run.stage, run.exit_code = "FAILED", "RUNTIME", 1
                    run.finished_at = utc_now()
                    run.pid = None
                elif job is not None:
                    # A process that exits without writing even its first
                    # event is a launch-boundary failure, not a product or
                    # provider result.  One bounded retry is safe here: no
                    # runtime event means no candidate claim and no Provider
                    # POST could have been committed.  If the retry also
                    # produces no event, keep the failure explicit and stop.
                    if (
                        not self._runtime_event_seen(session, run.job_id, since=launch_time)
                        and run.launch_attempts < MAX_EMPTY_LAUNCH_RETRIES + 1
                    ):
                        run.error = None
                        run.exit_code = None
                        run.finished_at = None
                        run.progress_note = (
                            "Worker 启动未输出事件，已自动重试一次；"
                            "不会重复候选或 Provider 调用"
                        )
                        self._launch(
                            session,
                            job,
                            run,
                            resume=self._command_is_resume(run.command_json),
                        )
                    else:
                        run.status, run.stage, run.exit_code = "FAILED", "RUNTIME", 1
                        run.error = "Website Native Runtime exited before a terminal workflow event"
                        job.status, job.current_stage, job.last_reason = "FAILED", "RUNTIME", run.error
                        run.finished_at = utc_now()
                        run.pid = None
            session.commit()
            return self._run_dict(run)
        finally:
            session.close()

    def reconcile_all(self) -> None:
        session = self.database.session_factory()
        try:
            run_ids = [run.run_id for run in session.scalars(select(ProductionRun).where(ProductionRun.status == "RUNNING"))]
        finally:
            session.close()
        for run_id in run_ids:
            self.reconcile_run(run_id)
        self._promote_next()

    def _promote_next(self) -> None:
        session = self.database.session_factory()
        try:
            if self._active_run(session) is not None:
                return
            queued = session.scalar(select(ProductionRun).where(ProductionRun.status == "QUEUED").order_by(ProductionRun.queue_position.asc(), ProductionRun.created_at.asc()))
            if queued is None:
                return
            job = session.get(ProductionJob, queued.job_id)
            if job is None:
                queued.status = "FAILED"
                queued.error = "job missing"
                session.commit()
                return
            self._launch(session, job, queued, resume=queued.stage == "RESUME")
            session.commit()
        finally:
            session.close()

    def _launch(self, session, job: ProductionJob, run: ProductionRun, *, resume: bool = False) -> None:
        workspace = Path(run.workspace or self._workspace(job))
        contract_path, events_path, _ = self._contract(session, job, workspace)
        command = [
            str(self.python_executable),
            "-u",
            str(self.native_entrypoint),
            "run-job",
            "--contract", str(contract_path),
            "--events", str(events_path),
            "--workspace", str(workspace),
        ]
        if resume:
            command.append("--resume")
        cwd = self.native_entrypoint.parents[1]
        env = os.environ.copy()
        website_root = str(self.native_entrypoint.parents[1])
        api_root = str(self.native_entrypoint.parents[1] / "services" / "api")
        engine_root = str(self.native_entrypoint.parents[1] / "packages" / "workflow-engine" / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (website_root, api_root, engine_root, env.get("PYTHONPATH", "")) if part
        )
        log_path = workspace / "website_runtime_launcher.log"
        # Capture the launch boundary before spawning so even a very fast
        # worker cannot write an event before the persisted timestamp used by
        # resume reconciliation.
        launch_time = utc_now()
        run.launch_attempts = int(run.launch_attempts or 0) + 1
        try:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"[{launch_time.isoformat()}] spawning Website Native Runtime "
                    f"attempt={run.launch_attempts} command={json.dumps(command, ensure_ascii=False)}\n"
                )
                log.flush()
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=creationflags,
                )
                log.write(f"[{utc_now().isoformat()}] spawned pid={process.pid}\n")
                log.flush()
        except OSError as exc:
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"[{utc_now().isoformat()}] spawn failed: {type(exc).__name__}: {exc}\n")
            except OSError:
                pass
            run.status, run.stage, run.error, run.exit_code = "FAILED", "RUNTIME", str(exc), 1
            run.finished_at = utc_now()
            job.status, job.current_stage, job.last_reason = "FAILED", "RUNTIME", str(exc)
            return
        run.status = "RUNNING"
        run.stage = "PREFLIGHT"
        run.progress_note = "Website Native Runtime started"
        run.pid = process.pid
        run.command_json = json.dumps(command, ensure_ascii=False)
        run.events_path = str(events_path)
        run.contract_path = str(contract_path)
        run.checkpoint_path = str(workspace / "checkpoint.json")
        # Each resume is a fresh worker launch for the same durable Run.  Keep
        # the original created_at for history, but reset the launch timestamps
        # so startup reconciliation does not use a stale timestamp from the
        # previous attempt.
        run.started_at = launch_time
        run.claimed_at = launch_time
        run.heartbeat_at = utc_now()
        job.status = "RUNNING"
        job.current_stage = "PREFLIGHT"
        job.last_reason = "Website Native Runtime 已启动"
        job.candidate_pool_path = str(workspace / "candidate_pool.json")
        session.add(run)

    def start(self, job_id: str) -> dict[str, Any]:
        self.reconcile_all()
        session = self.database.session_factory()
        try:
            job = session.get(ProductionJob, job_id)
            if job is None:
                raise KeyError(job_id)
            latest = self._latest_run(session, job_id)
            if latest is not None and latest.status == "RUNNING":
                return {"started": False, "reason": "ALREADY_RUNNING", "run": self._run_dict(latest)}
            if latest is not None and latest.status in {"BLOCKED", "HUMAN_REQUIRED"}:
                return {"started": False, "reason": "RESUME_REQUIRED", "run": self._run_dict(latest)}
            policy = _payload(job.policy_json)
            if not list(policy.get("category_ids") or []):
                raise ValueError("开始生产前必须持久化至少一个已选类目/范围")
            if job.target_mode != "ALL" and not job.target_value:
                raise ValueError("Exact-N / Up-To-N 必须有有效 target_value")
            if not self._provider_ready(session, job):
                raise ValueError("Provider 非 OFF：尚未收到 Website safety receipt（幂等性与 SUBMISSION_UNKNOWN 防重提），生产保持阻断")
            workspace = self._workspace(job)
            if self._active_run(session) is not None:
                position = int(session.scalar(select(func.count()).select_from(ProductionRun).where(ProductionRun.status == "QUEUED")) or 0) + 1
                run = ProductionRun(run_id=f"run_{uuid4().hex}", job_id=job_id, status="QUEUED", stage="QUEUE", progress_note="等待唯一生产槽位", workspace=str(workspace), queue_position=position)
                session.add(run)
                job.status, job.current_stage, job.last_reason = "QUEUED", "QUEUE", "已进入持久化生产队列"
            else:
                run = ProductionRun(run_id=f"run_{uuid4().hex}", job_id=job_id, status="QUEUED", stage="QUEUE", progress_note="等待启动", workspace=str(workspace), queue_position=0)
                session.add(run)
                session.flush()
                self._launch(session, job, run, resume=False)
            session.commit()
            return {"started": run.status == "RUNNING", "reason": None if run.status == "RUNNING" else "QUEUED", "run": self._run_dict(run)}
        finally:
            session.close()

    def status(self, job_id: str) -> dict[str, Any] | None:
        self.reconcile_all()
        session = self.database.session_factory()
        try:
            return self._run_dict(self._latest_run(session, job_id))
        finally:
            session.close()

    def resume(self, job_id: str) -> dict[str, Any]:
        self.reconcile_all()
        session = self.database.session_factory()
        try:
            job = session.get(ProductionJob, job_id)
            if job is None:
                raise KeyError(job_id)
            latest = self._latest_run(session, job_id)
            if latest is None:
                raise ValueError("该任务尚无可恢复的 Website Native Runtime 运行记录")
            if latest.status == "RUNNING":
                return {"started": False, "reason": "ALREADY_RUNNING", "run": self._run_dict(latest)}
            if latest.status in {"SUCCEEDED", "CANCELLED"}:
                return {"started": False, "reason": "NOT_RESUMABLE", "run": self._run_dict(latest)}
            if not self._provider_ready(session, job):
                raise ValueError("Provider 非 OFF：尚未收到 Website safety receipt（幂等性与 SUBMISSION_UNKNOWN 防重提），恢复保持阻断")

            latest.status = "QUEUED"
            latest.stage = "RESUME"
            latest.progress_note = "等待恢复同一 Job 的 Website Native Runtime"
            latest.error = None
            latest.exit_code = None
            latest.finished_at = None
            latest.pid = None
            # Count retries per user-requested launch, not across every
            # resume of the durable Job.
            latest.launch_attempts = 0
            if self._active_run(session) is not None:
                latest.queue_position = int(session.scalar(select(func.count()).select_from(ProductionRun).where(ProductionRun.status == "QUEUED")) or 0)
                job.status, job.current_stage, job.last_reason = "QUEUED", "RESUME", "已进入持久化恢复队列"
            else:
                latest.queue_position = 0
                session.flush()
                self._launch(session, job, latest, resume=True)
            session.commit()
            return {"started": latest.status == "RUNNING", "reason": None if latest.status == "RUNNING" else "QUEUED", "run": self._run_dict(latest)}
        finally:
            session.close()

    def cancel(self, job_id: str, reason: str = "operator_cancelled") -> dict[str, Any] | None:
        self.reconcile_all()
        session = self.database.session_factory()
        try:
            run = self._latest_run(session, job_id)
            job = session.get(ProductionJob, job_id)
            if run is None or job is None:
                return None
            if run.status == "RUNNING" and _process_alive(run.pid, _expected_executable(run.command_json)):
                if run.events_path and run.contract_path:
                    subprocess.run(
                        [str(self.python_executable), str(self.native_entrypoint), "cancel-job", "--contract", str(run.contract_path), "--events", str(run.events_path)],
                        cwd=str(self.native_entrypoint.parents[1]),
                        env=os.environ.copy(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                _terminate_process(run.pid, _expected_executable(run.command_json))
                self._ingest_file(session, run)
            job.status, job.current_stage, job.last_reason = "CANCELLED", "CANCELLED", reason
            run.status, run.stage, run.exit_code = "CANCELLED", "CANCELLED", 130
            run.finished_at, run.pid = utc_now(), None
            session.commit()
            return self._run_dict(run)
        finally:
            session.close()


__all__ = ["ProductionRuntimeService"]
