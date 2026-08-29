"""Canonical Website production worker.

This entrypoint is intentionally thin: the shared ProductionWorkflowEngine in
``workers.production_pipeline`` owns the control loop and durable checkpoints.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _existing_sequence(events_path: Path) -> int:
    if not events_path.is_file():
        return 0
    value = 0
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = max(value, int(payload.get("sequence") or 0))
    return value


def _emit(
    events_path: Path,
    contract: dict[str, Any],
    event_type: str,
    *,
    status: str,
    stage: str,
    message: str,
    done: int | None = None,
    total: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    sequence = _existing_sequence(events_path) + 1
    event = {
        "schema_version": "workflow-event.v2",
        "event_id": f"{contract['job_id']}:{sequence}",
        "job_id": contract["job_id"],
        "sequence": sequence,
        "type": event_type,
        "status": status,
        "stage": stage,
        "message": message,
        "done": done,
        "total": total,
        "payload": payload or {},
        "created_at": _timestamp(),
    }
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _fixture_enabled(contract: dict[str, Any]) -> bool:
    flag = os.getenv("FURNITURE_WORKFLOW_TEST_FIXTURES", "").strip().lower() in {"1", "true", "yes", "on"}
    return flag and isinstance(contract.get("fixture"), dict)


def _local_e2e_enabled(contract: dict[str, Any]) -> bool:
    flag = os.getenv("FURNITURE_WORKFLOW_LOCAL_E2E", "").strip().lower() in {"1", "true", "yes", "on"}
    return flag and str(contract.get("test_profile") or "").strip().upper() == "LOCAL_E2E"


def _env_dotlocal() -> dict[str, str]:
    dotlocal = Path(__file__).resolve().parent.parent / ".env.local"
    values: dict[str, str] = {}
    if not dotlocal.is_file():
        return values
    for line in dotlocal.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _event_status(event_type: str) -> str:
    if event_type == "JOB_COMPLETED":
        return "SUCCEEDED"
    if event_type == "JOB_BLOCKED":
        return "BLOCKED"
    if event_type in {"HUMAN_REQUIRED", "TARGET_SHORTAGE"}:
        return event_type
    return "RUNNING"


def run_job(contract_path: Path, events_path: Path, workspace: Path, *, resume: bool) -> int:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    for key, value in _env_dotlocal().items():
        os.environ.setdefault(key, value)
    _emit(
        events_path,
        contract,
        "JOB_RESUMED" if resume else "JOB_STARTED",
        status="RUNNING",
        stage="RESUME" if resume else "PREFLIGHT",
        message="Website Production Engine 恢复同一 Job/checkpoint" if resume else "Website Production Engine 已启动",
        payload={"engine": "ProductionWorkflowEngine", "interface": "WebsiteWorkflowInterface"},
    )
    local_e2e = _local_e2e_enabled(contract)
    if _fixture_enabled(contract) and not local_e2e:
        _emit(events_path, contract, "JOB_BLOCKED", status="BLOCKED", stage="FIXTURE_GATE", message="测试 fixture 不能进入真实交付", payload={"blocker": "FIXTURE_ONLY", "provider_calls": 0})
        return 2
    local_review = local_e2e or os.getenv("LOCAL_REVIEW_MODE", "").strip().casefold() == "agent"
    model_mode = os.getenv("WEBSITE_MODEL_MODE", "").strip() or os.getenv("LUNAMAX_MODEL_MODE", "").strip() or ("LOCAL_AGENT" if local_review else "TEXT_BRAIN_PLUS_VISION")
    _emit(
        events_path,
        contract,
        "BRAIN_BOUNDARY_CHECKED",
        status="RUNNING",
        stage="BRAIN_DECISION",
        message="Website 使用声明式 Review Provider；本地 Agent 模式不发起外部 Brain 请求" if local_review else "Website Brain 使用独立 WEBSITE_BRAIN_* 命名空间",
        payload={
            "namespace": "WEBSITE_BRAIN_*",
            "model_mode": model_mode,
            "review_provider": "LOCAL_AGENT" if local_review else "CONFIGURED_PROVIDER",
            "external_api_calls": 0 if local_review else "CONFIGURED_ONLY",
        },
    )
    from production_pipeline import ProductionPipeline

    def emit(event_type: str, stage: str, message: str, done: int | None, total: int | None, payload: dict[str, Any] | None) -> None:
        _emit(
            events_path,
            contract,
            event_type,
            status=_event_status(event_type),
            stage=stage,
            message=message,
            done=done,
            total=total,
            payload=payload,
        )

    pipeline_kwargs: dict[str, Any] = {}
    if local_e2e:
        from workers.local_e2e import build_local_e2e_components

        pipeline_kwargs = build_local_e2e_components(contract, workspace)
    try:
        return ProductionPipeline(contract=contract, workspace=workspace, emit=emit, **pipeline_kwargs).run()
    except Exception as error:
        _emit(
            events_path,
            contract,
            "JOB_FAILED",
            status="FAILED",
            stage="RUNTIME",
            message=f"Website Production Engine 失败：{type(error).__name__}",
            payload={"reason_code": type(error).__name__.upper(), "provider_calls": 0},
        )
        return 1


def cancel_job(contract_path: Path, events_path: Path) -> int:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _emit(events_path, contract, "JOB_CANCELLED", status="CANCELLED", stage="CANCELLED", message="操作员取消；Job、Candidate Pool、浏览器会话与 Provider ledger 保留")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Website production runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-job")
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--events", type=Path, required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--resume", action="store_true")
    cancel = sub.add_parser("cancel-job")
    cancel.add_argument("--contract", type=Path, required=True)
    cancel.add_argument("--events", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run-job":
        return run_job(args.contract, args.events, args.workspace, resume=args.resume)
    return cancel_job(args.contract, args.events)


if __name__ == "__main__":
    raise SystemExit(main())
