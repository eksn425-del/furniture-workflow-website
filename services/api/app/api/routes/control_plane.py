"""Operator-facing Website 1.0 control-plane endpoints.

This module deliberately contains orchestration and presentation state only.
It never implements a second crawler or provider client.  Live acquisition and
generation are implemented by Website Native Agentless workers; Skills is not loaded at runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import delete, func, select

from app.core.config import feature_flag
from app.models import (
    AIAssistantDecisionReceipt,
    ControlAuditLog,
    ProductionJob,
    ProductionJobEvent,
    ProductionRun,
    ProductionArtifact,
    ProductionProviderTask,
    ProviderSafetyCheck,
    ReviewQueueItem,
    SiteCategory,
    SiteProfile,
    SiteRegistryRecord,
    SiteScanRun,
    SiteTaxonomySnapshot,
    SiteEntryURL,
    utc_now,
)
from app.schemas import CompanyTestSiteRequest, ControlJobApproval, ControlJobCreate, ControlJobEdit, ControlJobStart, ControlJobTargetPatch, HumanReviewAction
from app.services.brain_provider import BrainError, BrainNotConfigured


router = APIRouter(prefix="/control", tags=["control-plane"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _site_key(url: str) -> str:
    parsed = urlsplit(url)
    return (parsed.hostname or "unknown-site").lower().removeprefix("www.")


def _display_name(site_key: str) -> str:
    known = {"roomandboard.com": "Room & Board", "cgtrader.com": "CGTrader"}
    return known.get(site_key, site_key.replace(".", " ").title())


def _source_kind(site_key: str) -> str:
    if site_key == "cgtrader.com":
        return "MODEL_MARKETPLACE"
    if site_key == "roomandboard.com":
        return "BRAND_DIRECT"
    return "UNKNOWN"


def _session(request: Request):
    return request.app.state.database.session_factory()


def _runtime(request: Request):
    return request.app.state.production_runtime


def _record_entry_url(session, site: SiteRegistryRecord, url: str) -> SiteEntryURL:
    entry = session.scalar(select(SiteEntryURL).where(SiteEntryURL.site_key == site.site_key, SiteEntryURL.url == url))
    if entry is None:
        entry = SiteEntryURL(entry_url_id=f"entry_{uuid4().hex}", site_key=site.site_key, url=url)
        session.add(entry)
    else:
        entry.last_seen_at = utc_now()
    return entry


def _demo_category_fixture() -> Path | None:
    """Return an explicit fixture only when the operator opts into demo mode."""
    if os.getenv("CONTROL_PLANE_DEMO_FIXTURES", "").strip() not in {"1", "true", "TRUE"}:
        return None
    candidate = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "taxonomy_demo.v2.json"
    return candidate if candidate.is_file() else None


def _ensure_site(session, source_url: str) -> SiteRegistryRecord:
    site_key = _site_key(source_url)
    site = session.get(SiteRegistryRecord, site_key)
    if site is None:
        site = SiteRegistryRecord(
            site_key=site_key,
            domain=site_key,
            display_name=_display_name(site_key),
            source_kind=_source_kind(site_key),
            source_health="UNKNOWN",
            acquisition_mode="WEBSITE_NATIVE",
            brand_policy="REVIEW",
            media_policy="EVIDENCE_ONLY",
            config_complexity="UNKNOWN",
            watermark_risk="UNKNOWN",
            profile_version="unverified",
            status="DRAFT",
        )
        session.add(site)
        session.flush()
    return site


def _event(session, job: ProductionJob, event_type: str, status: str, message: str, payload: dict | None = None) -> None:
    last_sequence = session.scalar(
        select(func.coalesce(func.max(ProductionJobEvent.sequence), 0)).where(
            ProductionJobEvent.job_id == job.job_id
        )
    ) or 0
    session.add(ProductionJobEvent(
        job_id=job.job_id,
        sequence=int(last_sequence) + 1,
        event_type=event_type,
        status=status,
        message=message[:1000],
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
    ))


def _audit(session, action: str, resource_type: str, resource_id: str, *, result: str = "RECORDED", payload: dict | None = None, actor: str = "system") -> None:
    session.add(ControlAuditLog(
        audit_id=f"audit_{uuid4().hex}",
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
    ))


def _policy(job: ProductionJob) -> dict:
    try:
        value = json.loads(job.policy_json or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _job_dict(job: ProductionJob) -> dict:
    target = job.target_value if job.target_mode != "ALL" else None
    return {
        "job_id": job.job_id,
        "title": job.title,
        "source_url": job.source_url,
        "site_key": job.site_key,
        "site_name": _display_name(job.site_key),
        "goal": job.goal,
        "status": job.status,
        "current_stage": job.current_stage,
        "target_mode": job.target_mode,
        "target_value": target,
        "scope": job.scope,
        "category_allocation": job.category_allocation,
        "allocation_strategy": job.allocation_strategy,
        "spillover": job.spillover,
        "requested_count": job.requested_count,
        "counts": {
            "reported_count": job.reported_count,
            "discovered_count": job.discovered_count,
            "unique_count": job.unique_count,
            "eligible_count": job.eligible_count,
            "ready_count": job.ready_count,
            "delivered_count": job.delivered_count,
            "shortage_count": job.shortage_count,
        },
        "provider": job.provider,
        "provider_calls": job.provider_calls,
        "provider_safety": job.provider_safety,
        "provider_qualification_version": job.provider_qualification_version,
        "candidate_pool_path": job.candidate_pool_path,
        "last_reason": job.last_reason,
        "policy": _policy(job),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _category_dict(category: SiteCategory) -> dict:
    # Prefer persisted hierarchy; retain path fallback for pre-v7 rows.
    segments = [part for part in category.path.split("/") if part]
    level = int(category.level or (2 if len(segments) >= 2 else 1))
    parent_path = "/" + segments[0] if level == 2 else None
    return {
        "category_id": category.category_id,
        "site_key": category.site_key,
        "native_name": category.native_name,
        "canonical_name": category.canonical_name,
        "path": category.path,
        "source_url": category.source_url,
        "count_value": category.count_value,
        "count_kind": category.count_kind,
        "confidence": category.confidence,
        "evidence": json.loads(category.evidence_json) if category.evidence_json else [],
        "verified_at": category.verified_at.isoformat() if category.verified_at else None,
        "reported_count": category.reported_count,
        "discovered_count": category.discovered_count,
        "eligible_count": category.eligible_count,
        "selected": category.selected,
        "level": level,
        "parent_path": parent_path,
        "parent_category_id": category.parent_category_id,
        "scope_kind": category.scope_kind,
        "last_scanned_at": category.last_scanned_at.isoformat() if category.last_scanned_at else None,
    }


_BLOCKED_TAXONOMY_STATUSES = frozenset({
    "HUMAN_REQUIRED",
    "BROWSER_REQUIRED",
    "BROWSER_RUNTIME_NOT_INSTALLED",
    "FAILED",
    "BRAIN_NOT_CONFIGURED",
})


def _current_site_categories(session, site_key: str, *, latest_scan: SiteScanRun | None = None) -> list[SiteCategory]:
    """Return categories from the latest usable snapshot only.

    SiteCategory is intentionally retained after a failed or challenged scan so
    the historical evidence is not destroyed.  The control-plane read model
    must not expose those retained rows as the current taxonomy, however;
    otherwise a new-task page can silently present an old PARTIAL snapshot as
    verified data after the latest scan ended HUMAN_REQUIRED.
    """

    latest_snapshot = session.scalar(
        select(SiteTaxonomySnapshot)
        .where(SiteTaxonomySnapshot.site_key == site_key)
        .order_by(SiteTaxonomySnapshot.captured_at.desc())
    )
    if latest_snapshot and latest_snapshot.status in _BLOCKED_TAXONOMY_STATUSES:
        return []
    if latest_scan and latest_scan.finished_at and latest_scan.status in _BLOCKED_TAXONOMY_STATUSES:
        return []

    query = select(SiteCategory).where(SiteCategory.site_key == site_key)
    if latest_snapshot:
        query = query.where(SiteCategory.snapshot_id == latest_snapshot.snapshot_id)
    return list(session.scalars(query.order_by(SiteCategory.path)))


def _artifact_dict(artifact: ProductionArtifact) -> dict:
    return {
        "artifact_id": artifact.artifact_id,
        "job_id": artifact.job_id,
        "run_id": artifact.run_id,
        "artifact_type": artifact.artifact_type,
        "status": artifact.status,
        "relative_path": artifact.relative_path,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "item_count": artifact.item_count,
        "manifest_schema": artifact.manifest_schema,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }


@router.get("/overview")
def overview(request: Request) -> dict:
    _runtime(request).reconcile_all()
    session = _session(request)
    try:
        jobs = list(session.scalars(select(ProductionJob).order_by(ProductionJob.updated_at.desc())))
        open_reviews = int(session.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.status == "OPEN")) or 0)
        active = sum(job.status in {"RUNNING", "DISCOVERING", "MODELING", "QA"} for job in jobs)
        human_required = sum(job.status in {"HUMAN_REQUIRED", "WAITING_REVIEW", "TARGET_SHORTAGE"} for job in jobs)
        failed = sum(job.status in {"FAILED", "PRODUCTION_BLOCKED"} for job in jobs)
        provider_running = sum(job.status == "PROVIDER_RUNNING" for job in jobs)
        delivered = sum(job.delivered_count for job in jobs)
        return {
            "schema_version": "website-control-overview.v1",
            "generated_at": _now_iso(),
            "demo_data": False,
            "action_cards": {
                "running": active,
                "waiting_review": human_required + open_reviews,
                "human_required": human_required,
                "failed": failed,
                "provider_running": provider_running,
                "delivered_today": delivered,
            },
            "metrics": {
                "today_output": delivered,
                "success_rate": 0 if not jobs else round(sum(job.status == "COMPLETED" for job in jobs) / len(jobs) * 100),
                "average_duration_hours": 0,
                "cost_minor": 0,
                "workers_online": 0,
            },
            "jobs": [_job_dict(job) for job in jobs[:20]],
            "reviews": [
                {
                    "review_id": item.review_id,
                    "job_id": item.job_id,
                    "reason_code": item.reason_code,
                    "title": item.title,
                    "detail": item.detail,
                    "severity": item.severity,
                    "status": item.status,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in session.scalars(select(ReviewQueueItem).where(ReviewQueueItem.status == "OPEN").order_by(ReviewQueueItem.created_at.desc()).limit(10))
            ],
            "provider_queue": {"running": provider_running, "waiting": 0, "capacity": 0, "unknown": 0},
        }
    finally:
        session.close()


@router.post("/site/preflight")
def control_site_preflight(payload: CompanyTestSiteRequest, request: Request) -> dict:
    output_dir = Path(request.app.state.settings.output_root) / "_control" / "preflight"
    try:
        return request.app.state.site_analyzer.preflight(str(payload.url), live=payload.live, output_dir=output_dir)
    except ValueError as error:
        return {
            "schema_version": "website-site-preflight.v2",
            "url": str(payload.url),
            "status": "INVALID_INPUT",
            "network_called": False,
            "provider_posts": 0,
            "next_action": str(error),
        }


@router.get("/jobs")
def list_control_jobs(request: Request) -> dict:
    _runtime(request).reconcile_all()
    session = _session(request)
    try:
        jobs = list(session.scalars(select(ProductionJob).order_by(ProductionJob.updated_at.desc())))
        items = []
        for job in jobs:
            item = _job_dict(job)
            item["run"] = _runtime(request).status(job.job_id)
            items.append(item)
        return {"items": items, "total": len(items)}
    finally:
        session.close()


@router.post("/jobs", status_code=201)
def create_control_job(payload: ControlJobCreate, request: Request) -> dict:
    session = _session(request)
    try:
        source_url = str(payload.source_url)
        site = _ensure_site(session, source_url)
        _record_entry_url(session, site, source_url)
        requested_count = payload.target_value if payload.target_mode != "ALL" and payload.target_value else 0
        policy = {
            "schema_version": "job-policy.v1",
            "source": "user_form",
            "target_mode": payload.target_mode,
            "target_value": payload.target_value,
            "scope": payload.scope,
            "category_allocation": payload.category_allocation,
            "allocation_strategy": payload.allocation_strategy,
            "spillover": payload.spillover,
            "category_ids": payload.category_ids,
            "provider": payload.provider,
            "provider_calls": 0,
        }
        job = ProductionJob(
            job_id=f"job_{uuid4().hex}",
            source_url=source_url,
            site_key=site.site_key,
            title=payload.title,
            goal=payload.goal,
            target_mode=payload.target_mode,
            target_value=payload.target_value,
            scope=payload.scope,
            category_allocation=payload.category_allocation,
            allocation_strategy=payload.allocation_strategy,
            spillover=payload.spillover,
            status="DRAFT",
            current_stage="URL_AND_GOAL",
            requested_count=requested_count,
            provider=payload.provider,
            provider_safety="NOT_CHECKED",
            policy_json=json.dumps(policy, ensure_ascii=False),
            last_reason="任务已创建，等待站点预检与类目扫描",
        )
        session.add(job)
        session.flush()
        _event(session, job, "JOB_CREATED", job.status, "任务已创建，等待 Website Native SiteScanRun", {"provider_posts": 0})
        _audit(session, "CREATE_JOB", "production_job", job.job_id, payload={"site_key": site.site_key})
        session.commit()
        return {"job": _job_dict(job), "site": {"site_key": site.site_key, "display_name": site.display_name, "status": site.status}}
    finally:
        session.close()


@router.get("/jobs/{job_id}")
def get_control_job(job_id: str, request: Request) -> dict:
    _runtime(request).status(job_id)
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        categories = list(session.scalars(select(SiteCategory).where(SiteCategory.site_key == job.site_key).order_by(SiteCategory.path)))
        events = list(session.scalars(select(ProductionJobEvent).where(ProductionJobEvent.job_id == job.job_id).order_by(ProductionJobEvent.sequence)))
        artifacts = list(session.scalars(select(ProductionArtifact).where(ProductionArtifact.job_id == job.job_id).order_by(ProductionArtifact.created_at)))
        provider_tasks = list(session.scalars(select(ProductionProviderTask).where(ProductionProviderTask.job_id == job.job_id).order_by(ProductionProviderTask.created_at)))
        latest_scan = session.scalar(select(SiteScanRun).where(SiteScanRun.job_id == job.job_id).order_by(SiteScanRun.started_at.desc()))
        candidate_pool: dict = {}
        if job.candidate_pool_path:
            pool_path = Path(job.candidate_pool_path)
            if pool_path.is_file():
                try:
                    raw_pool = json.loads(pool_path.read_text(encoding="utf-8"))
                    states: dict[str, int] = {}
                    for item in (raw_pool.get("items") or {}).values():
                        if isinstance(item, dict):
                            state = str(item.get("state") or "UNKNOWN")
                            states[state] = states.get(state, 0) + 1
                    summaries = []
                    for item in (raw_pool.get("items") or {}).values():
                        if not isinstance(item, dict):
                            continue
                        lineage = item.get("lineage") if isinstance(item.get("lineage"), dict) else {}
                        product_name = str(item.get("product_name") or "")
                        summaries.append({
                            "candidate_id": item.get("candidate_id"),
                            "record_id": item.get("record_id"),
                            "state": item.get("state"),
                            "product_name": product_name,
                            "name_char_count": len(product_name),
                            "name_limit": 50,
                            "production_gate_status": lineage.get("production_gate_status"),
                            "production_gate_reasons": lineage.get("production_gate_reasons") or [],
                            "review_provider": lineage.get("review_provider"),
                            "media_binding_status": lineage.get("media_binding_status"),
                            "identity_conflicts": lineage.get("identity_conflicts") or [],
                        })
                    candidate_pool = {"path": str(pool_path), "job_status": raw_pool.get("job_status"), "target_count": raw_pool.get("target_count"), "state_counts": states, "items": summaries, "updated_at": raw_pool.get("updated_at")}
                except (OSError, json.JSONDecodeError):
                    candidate_pool = {"path": str(pool_path), "status": "UNREADABLE"}
        return {
            "job": _job_dict(job),
            "run": _runtime(request).status(job.job_id),
            "categories": [_category_dict(item) for item in categories],
            "artifacts": [_artifact_dict(item) for item in artifacts],
            "site_scan": request.app.state.site_scan_runtime.status(latest_scan.scan_id) if latest_scan else None,
            "candidate_pool": candidate_pool,
            "provider_tasks": [{
                "ledger_id": item.ledger_id,
                "candidate_id": item.candidate_id,
                "record_id": item.record_id,
                "provider": item.provider,
                "provider_task_id": item.provider_task_id,
                "status": item.status,
                "checkpoint_state": item.checkpoint_state,
                "error_code": item.error_code,
                "error_message": item.error_message,
                "post_attempts": item.post_attempts,
                "poll_attempts": item.poll_attempts,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            } for item in provider_tasks],
            "events": [
                {
                    "sequence": item.sequence,
                    "event_type": item.event_type,
                    "schema_version": item.schema_version or "control-event.v1",
                    "status": item.status,
                    "stage": item.stage,
                    "items_done": item.items_done,
                    "items_total": item.items_total,
                    "message": item.message,
                    "payload": json.loads(item.payload_json) if item.payload_json else {},
                    "created_at": item.created_at.isoformat(),
                }
                for item in events
            ],
        }
    finally:
        session.close()


@router.post("/jobs/{job_id}/taxonomy/scan")
def scan_taxonomy(job_id: str, request: Request, payload: dict | None = None) -> dict:
    """Queue one durable Website SiteScanRun and attach it to this Job."""
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        site = _ensure_site(session, job.source_url)
        _record_entry_url(session, site, job.source_url)
        session.commit()
        return request.app.state.site_scan_runtime.start(
            site_key=site.site_key,
            source_url=job.source_url,
            job_id=job.job_id,
            live=bool((payload or {}).get("live", True)),
        )
    finally:
        session.close()


@router.post("/jobs/{job_id}/target")
def update_target(job_id: str, payload: ControlJobTargetPatch, request: Request) -> dict:
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        if payload.action == "ACCEPT_SHORTAGE":
            job.status = "COMPLETED" if job.delivered_count else "PLAN_READY"
            job.current_stage = "PRODUCTION_PLAN"
            job.last_reason = payload.reason or "用户确认按当前合格数量继续"
        elif payload.action == "ADD_CATEGORY":
            rows = list(session.scalars(select(SiteCategory).where(
                SiteCategory.site_key == job.site_key,
                SiteCategory.category_id.in_(payload.category_ids),
            ))) if payload.category_ids else []
            by_id = {row.category_id: row for row in rows}
            invalid = [value for value in payload.category_ids if value not in by_id]
            if invalid:
                raise HTTPException(status_code=422, detail=f"类目不属于当前站点：{', '.join(invalid[:5])}")
            selected_set = set(payload.category_ids)
            compacted = [
                category_id for category_id in payload.category_ids
                if not (by_id[category_id].parent_category_id and by_id[category_id].parent_category_id in selected_set)
            ]
            if not compacted:
                raise HTTPException(status_code=422, detail="至少选择一个有效类目/范围")
            for row in session.scalars(select(SiteCategory).where(SiteCategory.site_key == job.site_key)):
                row.selected = row.category_id in compacted
            policy = _policy(job)
            policy["category_ids"] = list(dict.fromkeys(compacted))
            policy["selection_compacted"] = len(compacted) != len(payload.category_ids)
            job.policy_json = json.dumps(policy, ensure_ascii=False)
            job.status = "POLICY_READY"
            job.current_stage = "TARGET_POLICY"
            job.last_reason = payload.reason or "用户添加了相关类目，等待重新预览"
        elif payload.action == "MODIFY_TARGET":
            if payload.target_value is None:
                raise HTTPException(status_code=422, detail="target_value is required")
            job.target_value = payload.target_value
            job.requested_count = payload.target_value
            job.status = "POLICY_READY"
            job.current_stage = "TARGET_POLICY"
            job.last_reason = payload.reason or "用户修改了目标数量"
        else:
            job.status = "STOPPED"
            job.current_stage = "STOPPED"
            job.last_reason = payload.reason or "用户停止任务"
        _event(session, job, "TARGET_DECISION", job.status, job.last_reason, {"action": payload.action, "category_ids": payload.category_ids, "target_value": payload.target_value})
        _audit(session, payload.action, "production_job", job.job_id, payload={"reason": payload.reason})
        session.commit()
        return {"job": _job_dict(job)}
    finally:
        session.close()


@router.post("/jobs/{job_id}/approve")
def approve_job(job_id: str, payload: ControlJobApproval, request: Request) -> dict:
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        if not payload.confirm:
            return {"status": "APPROVAL_REQUIRED", "job": _job_dict(job), "provider_calls": 0}
        if job.provider != "OFF":
            if payload.approved_cost_ceiling_minor <= 0:
                raise HTTPException(status_code=400, detail="Provider 非 OFF 时，审批必须填写大于 0 的成本上限（approved_cost_ceiling_minor）")
            provider = job.provider.casefold()
            configured = provider == "lux3d" and bool(os.getenv("LUX3D_API_KEY", "").strip())
            qualification = {
                "schema_version": "provider-qualification.v1",
                "provider": provider,
                "checks": {
                    "provider_supported": provider == "lux3d",
                    "credential_configured": configured,
                    "durable_idempotency_ledger": True,
                    "submission_unknown_quarantine": True,
                    "resume_by_known_task_id": True,
                    "provider_concurrency_one": True,
                    "explicit_cost_authorization": True,
                },
                "provider_posts": 0,
            }
            ready = all(bool(value) for value in qualification["checks"].values())
            authorization_payload = {
                "job_id": job.job_id,
                "provider": provider,
                "approved_cost_ceiling_minor": payload.approved_cost_ceiling_minor,
                "actor": payload.actor,
                "policy": _policy(job),
            }
            authorization_hash = hashlib.sha256(
                json.dumps(authorization_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            check = ProviderSafetyCheck(
                check_id=f"safety_{uuid4().hex}",
                job_id=job.job_id,
                provider=job.provider,
                provider_idempotency_safe=ready,
                submission_unknown_guard=ready,
                status="PRODUCTION_READY" if ready else "PRODUCTION_BLOCKED",
                reason=(
                    f"Provider {job.provider} qualification 与显式成本审批均通过"
                    if ready else
                    f"Provider {job.provider} qualification 未通过：请配置 LUX3D_API_KEY 或选择受支持 Provider"
                ),
                qualification_receipt_json=json.dumps(qualification, ensure_ascii=False),
                authorization_hash=authorization_hash,
            )
            session.add(check)
            job.provider_safety = check.status
            job.status = check.status
            job.current_stage = "PRODUCTION_PLAN"
            job.last_reason = check.reason
            job.provider_qualification_version = "provider-qualification.v1"
        else:
            job.provider_safety = "NOT_APPLICABLE_PROVIDER_OFF"
            job.status = "APPROVAL_RECORDED"
            job.current_stage = "PRODUCTION_PLAN"
            job.last_reason = "审批已记录；Provider 当前为 OFF，不会产生外部付费调用"
        job.plan_json = json.dumps({"approved_cost_ceiling_minor": payload.approved_cost_ceiling_minor, "actor": payload.actor, "authorization_hash": authorization_hash if job.provider != "OFF" else None, "provider_calls": 0}, ensure_ascii=False)
        _event(session, job, "APPROVAL_RECORDED", job.status, job.last_reason, {"actor": payload.actor, "provider_calls": 0})
        _audit(session, "APPROVE_PRODUCTION_PLAN", "production_job", job.job_id, actor=payload.actor, result=job.status)
        session.commit()
        return {"status": job.status, "provider_calls": 0, "job": _job_dict(job)}
    finally:
        session.close()


@router.get("/reviews")
def list_reviews(request: Request) -> dict:
    session = _session(request)
    try:
        items = list(session.scalars(select(ReviewQueueItem).where(ReviewQueueItem.status == "OPEN").order_by(ReviewQueueItem.created_at.desc())))
        return {
            "items": [{"review_id": item.review_id, "job_id": item.job_id, "reason_code": item.reason_code, "title": item.title, "detail": item.detail, "severity": item.severity, "status": item.status, "created_at": item.created_at.isoformat()} for item in items],
            "total": len(items),
        }
    finally:
        session.close()


@router.post("/reviews/{review_id}/action")
def act_on_review(review_id: str, payload: HumanReviewAction, request: Request) -> dict:
    session = _session(request)
    try:
        item = session.get(ReviewQueueItem, review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="review item not found")
        item.status = "RESOLVED" if payload.action != "STOP" else "STOPPED"
        item.updated_at = utc_now()
        if item.job_id:
            job = session.get(ProductionJob, item.job_id)
            if job:
                job.status = "STOPPED" if payload.action == "STOP" else "REVIEW_RESOLVED"
                job.last_reason = payload.reason or f"人工动作：{payload.action}"
                _event(session, job, "HUMAN_REVIEW", job.status, job.last_reason, {"review_id": review_id, "action": payload.action, "actor": payload.actor})
        _audit(session, "REVIEW_ACTION", "review_queue_item", review_id, actor=payload.actor, payload={"action": payload.action, "reason": payload.reason})
        session.commit()
        return {"status": item.status, "review_id": item.review_id, "action": payload.action}
    finally:
        session.close()


@router.get("/system")
def system_status(request: Request) -> dict:
    brain_status = request.app.state.website_brain.health()
    return {
        "schema_version": "website-system-status.v2",
        "website_brain": brain_status,
        "skills": {
            "runtime_mode": "frozen-reference-only",
            "root_configured": False,
            "bundled": False,
            "doctor": {"status": "NOT_REQUIRED", "modified_files": 0},
        },
        "provider": {"status": "OFF_BY_DEFAULT", "provider_calls": 0, "safety_gate": "RECEIPT_REQUIRED"},
        "database": {"engine": "sqlite-dev-or-configured", "status": "READY"},
        "object_storage": {"status": "NOT_CONFIGURED", "note": "Website 1.0 本地开发仍使用 OUTPUT_ROOT；生产对象存储需在部署环境配置。"},
        "workers": {"scrape": "WEBSITE_NATIVE", "site_scan": "WEBSITE_NATIVE", "modeling": "RECEIPT_GATED", "qa": "RECEIPT_GATED"},
        "runtime_agent_dependency": "NONE",
        "native_runtime": {"entrypoint": "Website/workers/native_runtime.py", "provider_posts": 0},
        "feature_flags": {"website_brain_enabled": feature_flag("WEBSITE_BRAIN_ENABLED", default=True), "native_runtime": True},
    }


@router.get("/jobs/{job_id}/events/stream")
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    async def events():
        try:
            last_sequence = max(0, int(request.headers.get("last-event-id") or 0))
        except (TypeError, ValueError):
            last_sequence = 0
        for _ in range(8):
            if await request.is_disconnected():
                return
            session = _session(request)
            try:
                job = session.get(ProductionJob, job_id)
                rows = list(session.scalars(select(ProductionJobEvent).where(ProductionJobEvent.job_id == job_id, ProductionJobEvent.sequence > last_sequence).order_by(ProductionJobEvent.sequence)))
                if job is None:
                    yield "event: error\ndata: {\"message\":\"production job not found\"}\n\n"
                    return
                for row in rows:
                    last_sequence = row.sequence
                    payload = {"schema_version": row.schema_version, "sequence": row.sequence, "event_type": row.event_type, "status": row.status, "stage": row.stage, "items_done": row.items_done, "items_total": row.items_total, "message": row.message, "created_at": row.created_at.isoformat()}
                    yield f"id: {row.sequence}\nevent: job_event\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
            finally:
                session.close()
            await asyncio.sleep(1.0)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sites")
def list_control_sites(request: Request) -> dict:
    """网站库：列出所有已导入站点，附带类目清单、数量与任务进度摘要。"""
    session = _session(request)
    try:
        sites = list(session.scalars(select(SiteRegistryRecord).order_by(SiteRegistryRecord.updated_at.desc())))
        items = []
        for site in sites:
            jobs = list(session.scalars(select(ProductionJob).where(ProductionJob.site_key == site.site_key)))
            latest_scan = session.scalar(
                select(SiteScanRun)
                .where(SiteScanRun.site_key == site.site_key)
                .order_by(SiteScanRun.started_at.desc())
            )
            categories = _current_site_categories(session, site.site_key, latest_scan=latest_scan)
            running = sum(job.status in {"RUNNING", "DISCOVERING", "MODELING", "QA", "PROVIDER_RUNNING"} for job in jobs)
            pending_review = sum(job.status in {"WAITING_REVIEW", "HUMAN_REQUIRED", "TARGET_SHORTAGE"} for job in jobs)
            items.append({
                "site_key": site.site_key,
                "domain": site.domain,
                "display_name": site.display_name,
                "source_kind": site.source_kind,
                "status": site.status,
                "profile_version": site.profile_version,
                "category_count": len(categories),
                "reported_total": sum(c.count_value or 0 for c in categories if c.count_kind != "UNKNOWN"),
                "unknown_count_categories": sum(c.count_kind == "UNKNOWN" for c in categories),
                "eligible_total": sum(c.eligible_count for c in categories),
                "job_count": len(jobs),
                "source_url": jobs[0].source_url if jobs else f"https://{site.domain}",
                "running_jobs": running,
                "pending_review_jobs": pending_review,
                "delivered_count": sum(job.delivered_count for job in jobs),
                "last_scanned_at": max((c.last_scanned_at.isoformat() for c in categories if c.last_scanned_at), default=None),
                # 首次打开可能落在扫描已入队、类目尚未落库的窗口；把最新扫描状态
                # 返回给前端，才能自动续轮询，而不是把瞬时空集合当成最终 0。
                "latest_scan_id": latest_scan.scan_id if latest_scan else None,
                "latest_scan_status": latest_scan.status if latest_scan else None,
                "latest_scan_finished_at": latest_scan.finished_at.isoformat() if latest_scan and latest_scan.finished_at else None,
                "latest_scan_error_code": latest_scan.error_code if latest_scan else None,
                "latest_scan_error_message": latest_scan.error_message if latest_scan else None,
                "categories": [_category_dict(c) for c in categories],
                "updated_at": site.updated_at.isoformat() if site.updated_at else None,
            })
        return {"schema_version": "website-sites.v1", "items": items, "total": len(items)}
    finally:
        session.close()


@router.get("/deliveries")
def list_deliveries(request: Request) -> dict:
    """List only receipt-backed deliveries emitted by the Website Native Runtime."""
    session = _session(request)
    try:
        artifacts = list(session.scalars(
            select(ProductionArtifact)
            .where(ProductionArtifact.status == "DELIVERED")
            .order_by(ProductionArtifact.updated_at.desc())
        ))
        grouped: dict[str, list[ProductionArtifact]] = {}
        for artifact in artifacts:
            grouped.setdefault(artifact.job_id, []).append(artifact)
        items = []
        for job_id, rows in grouped.items():
            items.append({
                "delivery_id": job_id,
                "job_id": job_id,
                "relative_path": "runtime-receipt",
                "batch_count": len(rows),
                "model_count": sum(item.item_count for item in rows),
                "modified_at": max(item.updated_at for item in rows).isoformat(),
                "batches": [
                    {
                        "artifact_id": item.artifact_id,
                        "name": item.artifact_type,
                        "file_count": item.item_count,
                        "download_path": item.artifact_id,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                        "manifest_schema": item.manifest_schema,
                    }
                    for item in rows
                ],
            })
        items.sort(key=lambda item: item["modified_at"], reverse=True)
        return {"schema_version": "website-deliveries.v2", "items": items, "total": len(items)}
    finally:
        session.close()


@router.post("/sites/{site_key}/scan")
def scan_site_without_job(site_key: str, request: Request, payload: dict | None = None) -> dict:
    """Queue one durable SiteScanRun without creating a Production Job."""
    session = _session(request)
    try:
        site = session.get(SiteRegistryRecord, site_key)
        body = payload or {}
        source_url = str(body.get("url") or (f"https://{site_key}" if site else ""))
        if site is None:
            if not source_url:
                raise HTTPException(status_code=404, detail="site not found; provide url")
            site = _ensure_site(session, source_url)
        _record_entry_url(session, site, source_url)
        session.commit()
        return request.app.state.site_scan_runtime.start(
            site_key=site.site_key,
            source_url=source_url,
            job_id=None,
            live=bool(body.get("live", True)),
        )
    finally:
        session.close()


@router.get("/sites/{site_key}")
def get_control_site(site_key: str, request: Request) -> dict:
    """Return one persisted site, taxonomy snapshots, scan history, and Jobs."""
    session = _session(request)
    try:
        site = session.get(SiteRegistryRecord, site_key)
        if site is None:
            raise HTTPException(status_code=404, detail="site not found")
        entries = list(session.scalars(select(SiteEntryURL).where(SiteEntryURL.site_key == site_key).order_by(SiteEntryURL.last_seen_at.desc())))
        snapshots = list(session.scalars(select(SiteTaxonomySnapshot).where(SiteTaxonomySnapshot.site_key == site_key).order_by(SiteTaxonomySnapshot.captured_at.desc()).limit(20)))
        scans = list(session.scalars(select(SiteScanRun).where(SiteScanRun.site_key == site_key).order_by(SiteScanRun.started_at.desc()).limit(50)))
        latest_scan = scans[0] if scans else None
        categories = _current_site_categories(session, site_key, latest_scan=latest_scan)
        jobs = list(session.scalars(select(ProductionJob).where(ProductionJob.site_key == site_key).order_by(ProductionJob.updated_at.desc())))
        profile = session.get(SiteProfile, site_key)
        return {
            "schema_version": "website-site-detail.v1",
            "site": {"site_key": site.site_key, "domain": site.domain, "display_name": site.display_name, "source_kind": site.source_kind, "source_health": site.source_health, "acquisition_mode": site.acquisition_mode, "status": site.status, "profile_version": site.profile_version, "last_verified_at": site.last_verified_at.isoformat() if site.last_verified_at else None, "created_at": site.created_at.isoformat(), "updated_at": site.updated_at.isoformat()},
            "profile": json.loads(profile.profile_json) if profile and profile.profile_json else None,
            "entry_urls": [{"url": item.url, "first_seen_at": item.first_seen_at.isoformat(), "last_seen_at": item.last_seen_at.isoformat(), "last_status": item.last_status, "last_taxonomy_snapshot_id": item.last_taxonomy_snapshot_id} for item in entries],
            "categories": [_category_dict(item) for item in categories],
            "snapshots": [{"snapshot_id": item.snapshot_id, "source_url": item.source_url, "status": item.status, "captured_at": item.captured_at.isoformat(), "evidence": json.loads(item.evidence_json) if item.evidence_json else {}} for item in snapshots],
            "scans": [{"scan_id": item.scan_id, "source_url": item.source_url, "status": item.status, "live": item.live, "taxonomy_level": item.taxonomy_level, "brain_status": item.brain_status, "provider_posts": item.provider_posts, "receipt_path": item.receipt_path, "error_code": item.error_code, "error_message": item.error_message, "started_at": item.started_at.isoformat(), "finished_at": item.finished_at.isoformat() if item.finished_at else None} for item in scans],
            "jobs": [_job_dict(item) for item in jobs],
        }
    finally:
        session.close()


@router.get("/sites/{site_key}/scans")
def list_site_scans(site_key: str, request: Request) -> dict:
    session = _session(request)
    try:
        if session.get(SiteRegistryRecord, site_key) is None:
            raise HTTPException(status_code=404, detail="site not found")
        scans = list(session.scalars(select(SiteScanRun).where(SiteScanRun.site_key == site_key).order_by(SiteScanRun.started_at.desc())))
        return {"schema_version": "website-site-scans.v1", "items": [{"scan_id": item.scan_id, "source_url": item.source_url, "status": item.status, "live": item.live, "taxonomy_level": item.taxonomy_level, "brain_status": item.brain_status, "provider_posts": item.provider_posts, "error_code": item.error_code, "error_message": item.error_message, "started_at": item.started_at.isoformat(), "finished_at": item.finished_at.isoformat() if item.finished_at else None} for item in scans], "total": len(scans)}
    finally:
        session.close()


@router.get("/scans/{scan_id}")
def get_site_scan(scan_id: str, request: Request) -> dict:
    result = request.app.state.site_scan_runtime.status(scan_id)
    if result is None:
        raise HTTPException(status_code=404, detail="site scan not found")
    return result


@router.post("/scans/{scan_id}/resume")
def resume_site_scan(scan_id: str, request: Request) -> dict:
    try:
        return request.app.state.site_scan_runtime.resume(scan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="site scan not found")


@router.delete("/sites/{site_key}")
def delete_control_site(site_key: str, request: Request) -> dict:
    """删除网站档案：类目、快照、扫描历史、入口 URL 与站点注册记录。

    关联的生产 Job 属于独立生命周期（任务页可单独删除），不随站点删除；
    但站点下仍有运行中的任务时拒绝删除，防止生产中断后无法追踪。
    """
    session = _session(request)
    try:
        site = session.get(SiteRegistryRecord, site_key)
        if site is None:
            raise HTTPException(status_code=404, detail="site not found")
        jobs = list(session.scalars(select(ProductionJob).where(ProductionJob.site_key == site_key)))
        running = [job for job in jobs if job.status in {"RUNNING", "DISCOVERING", "MODELING", "QA", "PROVIDER_RUNNING"}]
        if running:
            raise HTTPException(status_code=409, detail=f"该网站还有 {len(running)} 个运行中的任务，请先在任务列表停止或等待完成后再删除。")
        counts = {}
        for model in (SiteCategory, SiteTaxonomySnapshot, SiteScanRun, SiteEntryURL, SiteProfile):
            counts[model.__tablename__] = int(session.execute(delete(model).where(model.site_key == site_key), execution_options={"synchronize_session": False}).rowcount or 0)
        session.delete(site)
        _audit(session, "DELETE_SITE", "site_registry", site_key, result="DELETED", payload={"site_key": site_key, "deleted_rows": counts, "kept_jobs": len(jobs)})
        session.commit()
        return {"deleted": True, "site_key": site_key, "deleted_rows": counts, "kept_jobs": len(jobs)}
    finally:
        session.close()


@router.post("/jobs/{job_id}/taxonomy/refresh")
def refresh_taxonomy(job_id: str, request: Request) -> dict:
    """Refresh taxonomy through Website Native Site Analyzer while preserving prior snapshots."""
    return scan_taxonomy(job_id, request)


def _artifact_file(session, artifact: ProductionArtifact, output_root: Path) -> tuple[Path, str] | None:
    base = output_root.resolve()
    if artifact.run_id:
        run = session.get(ProductionRun, artifact.run_id)
        if run and run.workspace:
            base = Path(run.workspace).resolve()
    candidate = (base / artifact.relative_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate, base.name


@router.get("/deliveries/artifacts/{artifact_id}/download")
def download_delivery_artifact(artifact_id: str, request: Request) -> Response:
    """Download one receipt-backed artifact; incomplete artifacts stay disabled."""
    session = _session(request)
    try:
        artifact = session.get(ProductionArtifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="delivery artifact not found")
        if artifact.status != "DELIVERED":
            raise HTTPException(status_code=409, detail="delivery artifact is not complete")
        resolved = _artifact_file(session, artifact, Path(request.app.state.settings.output_root))
        if resolved is None:
            raise HTTPException(status_code=400, detail="invalid artifact path")
        path, _ = resolved
        if path.is_file():
            payload = path.read_bytes()
            media_type = "application/json" if artifact.artifact_type == "MANIFEST_JSON" else "text/csv" if artifact.artifact_type == "MANIFEST_CSV" else "application/octet-stream"
            filename = path.name
        elif path.is_dir() and artifact.artifact_type == "DELIVERY_FOLDER":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for item in sorted(path.rglob("*")):
                    if item.is_file():
                        archive.write(item, arcname=item.relative_to(path).as_posix())
            payload = buffer.getvalue()
            media_type = "application/zip"
            filename = f"{path.name}.zip"
        else:
            raise HTTPException(status_code=404, detail="delivery artifact file is missing")
        return Response(content=payload, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    finally:
        session.close()


@router.get("/deliveries/download")
def download_delivery_batch(batch_path: str, request: Request) -> Response:
    """Compatibility endpoint resolved through a persisted artifact record."""
    session = _session(request)
    try:
        artifact = session.scalar(select(ProductionArtifact).where(ProductionArtifact.artifact_id == batch_path))
        if artifact is None:
            raise HTTPException(status_code=404, detail="delivery artifact not found; use artifact_id from the receipt")
    finally:
        session.close()
    return download_delivery_artifact(artifact.artifact_id, request)


@router.post("/jobs/{job_id}/start")
def start_production_job(job_id: str, request: Request, payload: ControlJobStart | None = None) -> dict:
    """Queue or start the same durable Job Contract in Website Native Runtime."""
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        if job.provider == "OFF":
            job.provider_safety = "NOT_APPLICABLE_PROVIDER_OFF"
        job.last_reason = "已请求 Website Native Runtime；系统将按持久化队列执行"
        _event(session, job, "PRODUCTION_START_REQUESTED", job.status, job.last_reason, {"runtime": "Website/workers/native_runtime.py", "provider_posts": 0})
        _audit(session, "START_PRODUCTION", "production_job", job.job_id, actor="operator")
        session.commit()
    finally:
        session.close()
    try:
        result = _runtime(request).start(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="production job not found")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return result


@router.post("/jobs/{job_id}/cancel")
def cancel_production_job(job_id: str, request: Request) -> dict:
    """Cancel the runtime while preserving Job and Provider task identity."""
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        _event(session, job, "PRODUCTION_CANCEL_REQUESTED", "CANCELLED", "操作员请求取消；Job identity、checkpoint 与 Provider ledger 保留", {"provider_posts": 0})
        _audit(session, "CANCEL_PRODUCTION", "production_job", job.job_id, actor="operator")
        session.commit()
    finally:
        session.close()
    result = _runtime(request).cancel(job_id, reason="operator_cancelled")
    if result is None:
        raise HTTPException(status_code=404, detail="production job not found")
    return {"cancelled": True, "run": result}


@router.post("/jobs/{job_id}/resume")
def resume_production_job(job_id: str, request: Request) -> dict:
    """Resume the same persisted Job Contract and event stream."""
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        _event(session, job, "PRODUCTION_RESUME_REQUESTED", "QUEUED", "操作员请求恢复同一 Job；不创建新 Job", {"provider_posts": 0})
        _audit(session, "RESUME_PRODUCTION", "production_job", job.job_id, actor="operator")
        session.commit()
    finally:
        session.close()
    try:
        return _runtime(request).resume(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="production job not found")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.get("/jobs/{job_id}/run")
def get_production_run_endpoint(job_id: str, request: Request) -> dict:
    run = _runtime(request).status(job_id)
    if run is None:
        raise HTTPException(status_code=404, detail="该任务还没有生产运行记录")
    return {"run": run}


@router.delete("/jobs/{job_id}")
def delete_control_job(job_id: str, request: Request) -> dict:
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        running = session.scalar(
            select(ProductionRun).where(ProductionRun.job_id == job.job_id, ProductionRun.status == "RUNNING")
        )
        if running is not None:
            raise HTTPException(status_code=400, detail="该任务正在生产中，请先等待生产结束再删除")
        _audit(session, "DELETE_JOB", "production_job", job.job_id, actor="operator")
        session.delete(job)
        session.commit()
        return {"deleted": True, "job_id": job_id}
    finally:
        session.close()


@router.patch("/jobs/{job_id}")
def update_control_job(job_id: str, payload: ControlJobEdit, request: Request) -> dict:
    session = _session(request)
    try:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="production job not found")
        if payload.title is not None:
            title = " ".join(str(payload.title).split())
            if title:
                job.title = title
        if payload.goal is not None:
            goal = " ".join(str(payload.goal).split())
            if goal:
                job.goal = goal
        if payload.target_value is not None:
            job.target_value = payload.target_value
            job.requested_count = payload.target_value
        if payload.provider is not None and payload.provider != job.provider:
            job.provider = payload.provider
            job.provider_safety = "NOT_CHECKED"
            job.status = "POLICY_READY"
            job.current_stage = "PROVIDER_SAFETY_GATE"
            job.last_reason = f"Provider 已切换为 {payload.provider}，需重新审批后启动生产"
        _event(session, job, "JOB_EDITED", job.status, "操作员编辑了任务信息", {
            "title": payload.title is not None,
            "goal": payload.goal is not None,
            "target_value": payload.target_value is not None,
            "provider": payload.provider is not None,
        })
        _audit(session, "EDIT_JOB", "production_job", job.job_id, actor="operator")
        session.commit()
        return {"job": _job_dict(job)}
    finally:
        session.close()


__all__ = ["router"]
