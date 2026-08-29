from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


WORKFLOW_SCHEMA_VERSION = "workflow-state.v2"
LEGACY_WORKFLOW_SCHEMA_VERSION = "workflow-state.v1"


class BusinessStage(StrEnum):
    SCRAPE = "01_scrape"
    REPAIR = "02_repair"
    MODELS = "03_models"
    EXPORT = "04_export"
    INPUT = "05_input"
    QA = "06_qa"


class TaskKey(StrEnum):
    PROJECT_INGEST = "project_ingest"
    PRODUCT_DISCOVERY = "product_discovery"
    ASSET_ACQUISITION = "asset_acquisition"
    DIMENSION_EXTRACTION = "dimension_extraction"
    DIMENSION_AUDIT = "dimension_audit"
    DIMENSION_RESOLUTION = "dimension_resolution"
    CATALOG_LOCK = "catalog_lock"
    MODEL_GENERATION = "model_generation"
    BLENDER_EXPORT = "blender_export"
    GLB_VALIDATION = "glb_validation"
    ENTERPRISE_INPUT = "enterprise_input"
    LIBRARY_COMPARISON = "library_comparison"


TASK_SEQUENCE: tuple[TaskKey, ...] = tuple(TaskKey)


TASK_STAGE: dict[TaskKey, BusinessStage] = {
    TaskKey.PROJECT_INGEST: BusinessStage.SCRAPE,
    TaskKey.PRODUCT_DISCOVERY: BusinessStage.SCRAPE,
    TaskKey.ASSET_ACQUISITION: BusinessStage.SCRAPE,
    TaskKey.DIMENSION_EXTRACTION: BusinessStage.REPAIR,
    TaskKey.DIMENSION_AUDIT: BusinessStage.REPAIR,
    TaskKey.DIMENSION_RESOLUTION: BusinessStage.REPAIR,
    TaskKey.CATALOG_LOCK: BusinessStage.REPAIR,
    TaskKey.MODEL_GENERATION: BusinessStage.MODELS,
    TaskKey.BLENDER_EXPORT: BusinessStage.MODELS,
    TaskKey.GLB_VALIDATION: BusinessStage.EXPORT,
    TaskKey.ENTERPRISE_INPUT: BusinessStage.INPUT,
    TaskKey.LIBRARY_COMPARISON: BusinessStage.QA,
}


class JobStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    PAUSED_CHALLENGE = "paused_challenge"
    WAITING_PROVIDER = "waiting_provider"
    PROVIDER_CAPACITY_WAIT = "provider_capacity_wait"
    SUBMISSION_UNKNOWN = "submission_unknown"
    MANUAL_RECONCILIATION = "manual_reconciliation"
    WAITING_REFILL = "waiting_refill"
    WAITING_EXTERNAL_UPLOAD = "waiting_external_upload"
    WAITING_EXTERNAL_EXPORT = "waiting_external_export"
    SUPPLY_EXHAUSTED = "supply_exhausted"
    SOFTWARE_ERROR = "job_blocked_software_error"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TaskState:
    key: TaskKey
    status: JobStatus = JobStatus.PENDING
    attempt: int = 0
    updated_at: datetime = field(default_factory=utc_now)
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    sequence: int
    task: TaskKey
    from_status: JobStatus
    to_status: JobStatus
    actor: str
    reason: str
    at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class GateContext:
    catalog_lock_hash: str | None = None
    paid_generation_approval_hash: str | None = None
    export_approval_hash: str | None = None
    enterprise_input_present: bool = False


@dataclass(frozen=True, slots=True)
class StageView:
    key: BusinessStage
    status: JobStatus
    completed_tasks: int
    total_tasks: int
    active_task: TaskKey | None


@dataclass(frozen=True, slots=True)
class WorkflowState:
    tasks: tuple[TaskState, ...]
    events: tuple[WorkflowEvent, ...] = ()
    schema_version: str = WORKFLOW_SCHEMA_VERSION

    def task(self, key: TaskKey) -> TaskState:
        return next(item for item in self.tasks if item.key == key)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tasks"] = [
            {
                **item,
                "key": item["key"].value,
                "status": item["status"].value,
                "updated_at": item["updated_at"].isoformat(),
            }
            for item in payload["tasks"]
        ]
        payload["events"] = [
            {
                **item,
                "task": item["task"].value,
                "from_status": item["from_status"].value,
                "to_status": item["to_status"].value,
                "at": item["at"].isoformat(),
            }
            for item in payload["events"]
        ]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowState":
        version = payload.get("schema_version")
        if version not in {WORKFLOW_SCHEMA_VERSION, LEGACY_WORKFLOW_SCHEMA_VERSION}:
            raise ValueError("unsupported workflow state schema")
        raw_tasks = payload.get("tasks")
        raw_events = payload.get("events")
        if not isinstance(raw_tasks, list) or not isinstance(raw_events, list):
            raise ValueError("workflow state tasks/events must be lists")
        if version == LEGACY_WORKFLOW_SCHEMA_VERSION:
            raw_tasks = _migrate_v1_tasks(raw_tasks)
        tasks = tuple(
            TaskState(
                key=TaskKey(item["key"]),
                status=JobStatus(item["status"]),
                attempt=int(item.get("attempt", 0)),
                updated_at=datetime.fromisoformat(str(item["updated_at"]).replace("Z", "+00:00")),
                detail=item.get("detail"),
            )
            for item in raw_tasks
        )
        if {task.key for task in tasks} != set(TaskKey):
            raise ValueError("workflow state must contain every task exactly once")
        if len(tasks) != len(TaskKey):
            raise ValueError("workflow state contains duplicate tasks")
        events = tuple(
            WorkflowEvent(
                sequence=int(item["sequence"]),
                task=TaskKey(item["task"]),
                from_status=JobStatus(item["from_status"]),
                to_status=JobStatus(item["to_status"]),
                actor=str(item["actor"]),
                reason=str(item["reason"]),
                at=datetime.fromisoformat(str(item["at"]).replace("Z", "+00:00")),
            )
            for item in raw_events
        )
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise ValueError("workflow event sequence is invalid")
        return cls(tasks=tasks, events=events)


def _migrate_v1_tasks(raw_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upgrade control-plane state only; filesystem migration remains explicit."""
    migrated = [dict(item) for item in raw_tasks]
    library = next((item for item in migrated if item.get("key") == "library_comparison"), None)
    glb = next((item for item in migrated if item.get("key") == "glb_validation"), None)
    if library is None or glb is None:
        raise ValueError("legacy workflow state is missing required tasks")
    terminal_or_started = library.get("status") != JobStatus.PENDING.value
    input_status = (
        JobStatus.SUCCEEDED.value
        if terminal_or_started
        else JobStatus.READY.value
        if glb.get("status") == JobStatus.SUCCEEDED.value
        else JobStatus.PENDING.value
    )
    input_task = {
        "key": TaskKey.ENTERPRISE_INPUT.value,
        "status": input_status,
        "attempt": 0,
        "updated_at": library.get("updated_at") or glb.get("updated_at"),
        "detail": "migrated from workflow-state.v1; verify legacy folders before continuing",
    }
    index = next(index for index, item in enumerate(migrated) if item.get("key") == "library_comparison")
    migrated.insert(index, input_task)
    return migrated
