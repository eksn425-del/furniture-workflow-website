from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .model import (
    BusinessStage,
    GateContext,
    JobStatus,
    StageView,
    TASK_SEQUENCE,
    TASK_STAGE,
    TaskKey,
    TaskState,
    WorkflowEvent,
    WorkflowState,
    utc_now,
)


class InvalidTransition(ValueError):
    pass


class GateViolation(InvalidTransition):
    pass


ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.READY, JobStatus.CANCELLED}),
    JobStatus.READY: frozenset({JobStatus.RUNNING, JobStatus.BLOCKED, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.NEEDS_REVIEW,
            JobStatus.PAUSED_CHALLENGE,
            JobStatus.WAITING_PROVIDER,
            JobStatus.PROVIDER_CAPACITY_WAIT,
            JobStatus.SUBMISSION_UNKNOWN,
            JobStatus.MANUAL_RECONCILIATION,
            JobStatus.WAITING_REFILL,
            JobStatus.WAITING_EXTERNAL_UPLOAD,
            JobStatus.WAITING_EXTERNAL_EXPORT,
            JobStatus.SUPPLY_EXHAUSTED,
            JobStatus.SOFTWARE_ERROR,
            JobStatus.BLOCKED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.NEEDS_REVIEW: frozenset({JobStatus.READY, JobStatus.RUNNING, JobStatus.BLOCKED, JobStatus.CANCELLED}),
    JobStatus.PAUSED_CHALLENGE: frozenset({JobStatus.RUNNING, JobStatus.NEEDS_REVIEW, JobStatus.CANCELLED}),
    JobStatus.WAITING_PROVIDER: frozenset({JobStatus.RUNNING, JobStatus.NEEDS_REVIEW, JobStatus.CANCELLED}),
    JobStatus.PROVIDER_CAPACITY_WAIT: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.SUBMISSION_UNKNOWN: frozenset({JobStatus.MANUAL_RECONCILIATION, JobStatus.CANCELLED}),
    JobStatus.MANUAL_RECONCILIATION: frozenset({JobStatus.NEEDS_REVIEW, JobStatus.CANCELLED}),
    JobStatus.WAITING_REFILL: frozenset({JobStatus.RUNNING, JobStatus.SUPPLY_EXHAUSTED, JobStatus.CANCELLED}),
    JobStatus.WAITING_EXTERNAL_UPLOAD: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.WAITING_EXTERNAL_EXPORT: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.SUPPLY_EXHAUSTED: frozenset({JobStatus.READY, JobStatus.CANCELLED}),
    JobStatus.SOFTWARE_ERROR: frozenset({JobStatus.NEEDS_REVIEW, JobStatus.CANCELLED}),
    JobStatus.BLOCKED: frozenset({JobStatus.READY, JobStatus.CANCELLED}),
    JobStatus.FAILED: frozenset({JobStatus.READY, JobStatus.BLOCKED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class WorkflowEngine:
    def new(self, *, actor: str = "system", at: datetime | None = None) -> WorkflowState:
        timestamp = at or utc_now()
        tasks = tuple(
            TaskState(
                key=key,
                status=JobStatus.READY if index == 0 else JobStatus.PENDING,
                updated_at=timestamp,
            )
            for index, key in enumerate(TASK_SEQUENCE)
        )
        event = WorkflowEvent(
            sequence=1,
            task=TASK_SEQUENCE[0],
            from_status=JobStatus.PENDING,
            to_status=JobStatus.READY,
            actor=actor,
            reason="workflow initialized",
            at=timestamp,
        )
        return WorkflowState(tasks=tasks, events=(event,))

    def transition(
        self,
        state: WorkflowState,
        task_key: TaskKey,
        to_status: JobStatus,
        *,
        actor: str,
        reason: str,
        gates: GateContext | None = None,
        at: datetime | None = None,
    ) -> WorkflowState:
        if not actor.strip():
            raise InvalidTransition("actor is required")
        if not reason.strip():
            raise InvalidTransition("reason is required")

        current = state.task(task_key)
        if to_status not in ALLOWED_TRANSITIONS[current.status]:
            raise InvalidTransition(f"{task_key.value}: {current.status.value} -> {to_status.value} is not allowed")

        self._require_prerequisites(state, task_key, to_status)
        if to_status is JobStatus.RUNNING:
            self._require_gates(task_key, gates or GateContext())

        timestamp = at or utc_now()
        updated = replace(
            current,
            status=to_status,
            attempt=current.attempt + (1 if to_status is JobStatus.RUNNING else 0),
            updated_at=timestamp,
            detail=reason,
        )
        tasks = tuple(updated if item.key is task_key else item for item in state.tasks)
        event = WorkflowEvent(
            sequence=len(state.events) + 1,
            task=task_key,
            from_status=current.status,
            to_status=to_status,
            actor=actor,
            reason=reason,
            at=timestamp,
        )
        result = WorkflowState(tasks=tasks, events=state.events + (event,))
        return self._unlock_ready_tasks(result, actor=actor, at=timestamp) if to_status is JobStatus.SUCCEEDED else result

    def stage_views(self, state: WorkflowState) -> tuple[StageView, ...]:
        return tuple(self._stage_view(state, stage) for stage in BusinessStage)

    def _stage_view(self, state: WorkflowState, stage: BusinessStage) -> StageView:
        tasks = [item for item in state.tasks if TASK_STAGE[item.key] is stage]
        statuses = {item.status for item in tasks}
        if all(item.status is JobStatus.SUCCEEDED for item in tasks):
            status = JobStatus.SUCCEEDED
        elif statuses & {
            JobStatus.RUNNING, JobStatus.WAITING_PROVIDER, JobStatus.PROVIDER_CAPACITY_WAIT,
            JobStatus.WAITING_REFILL, JobStatus.WAITING_EXTERNAL_UPLOAD, JobStatus.WAITING_EXTERNAL_EXPORT,
        }:
            status = JobStatus.RUNNING
        elif statuses & {
            JobStatus.NEEDS_REVIEW, JobStatus.PAUSED_CHALLENGE, JobStatus.SUBMISSION_UNKNOWN,
            JobStatus.MANUAL_RECONCILIATION,
        }:
            status = JobStatus.NEEDS_REVIEW
        elif JobStatus.SUPPLY_EXHAUSTED in statuses:
            status = JobStatus.SUPPLY_EXHAUSTED
        elif JobStatus.SOFTWARE_ERROR in statuses:
            status = JobStatus.SOFTWARE_ERROR
        elif JobStatus.FAILED in statuses:
            status = JobStatus.FAILED
        elif JobStatus.BLOCKED in statuses:
            status = JobStatus.BLOCKED
        elif JobStatus.READY in statuses:
            status = JobStatus.READY
        elif all(item.status is JobStatus.CANCELLED for item in tasks):
            status = JobStatus.CANCELLED
        else:
            status = JobStatus.PENDING
        active = next(
            (item.key for item in tasks if item.status in {
                JobStatus.RUNNING, JobStatus.NEEDS_REVIEW, JobStatus.READY,
                JobStatus.PAUSED_CHALLENGE, JobStatus.WAITING_PROVIDER,
                JobStatus.PROVIDER_CAPACITY_WAIT, JobStatus.WAITING_REFILL,
            }),
            None,
        )
        return StageView(
            key=stage,
            status=status,
            completed_tasks=sum(item.status is JobStatus.SUCCEEDED for item in tasks),
            total_tasks=len(tasks),
            active_task=active,
        )

    @staticmethod
    def _require_prerequisites(state: WorkflowState, task_key: TaskKey, to_status: JobStatus) -> None:
        if to_status not in {JobStatus.READY, JobStatus.RUNNING}:
            return
        index = TASK_SEQUENCE.index(task_key)
        incomplete = [key.value for key in TASK_SEQUENCE[:index] if state.task(key).status is not JobStatus.SUCCEEDED]
        if incomplete:
            raise GateViolation(f"prerequisites are not complete: {', '.join(incomplete)}")

    @staticmethod
    def _require_gates(task_key: TaskKey, gates: GateContext) -> None:
        if task_key is TaskKey.MODEL_GENERATION:
            if not gates.catalog_lock_hash:
                raise GateViolation("model generation requires a locked catalog hash")
            if gates.paid_generation_approval_hash != gates.catalog_lock_hash:
                raise GateViolation("paid-generation approval does not match the catalog lock")
        elif task_key is TaskKey.BLENDER_EXPORT:
            if not gates.catalog_lock_hash:
                raise GateViolation("Blender export requires a locked catalog hash")
            if gates.export_approval_hash != gates.catalog_lock_hash:
                raise GateViolation("export approval does not match the catalog lock")
        elif task_key is TaskKey.ENTERPRISE_INPUT and not gates.enterprise_input_present:
            raise GateViolation("05_input requires an enterprise export input")

    def _unlock_ready_tasks(self, state: WorkflowState, *, actor: str, at: datetime) -> WorkflowState:
        tasks = list(state.tasks)
        events = list(state.events)
        for index, item in enumerate(tasks):
            if item.status is not JobStatus.PENDING:
                continue
            prior = TASK_SEQUENCE[: TASK_SEQUENCE.index(item.key)]
            if not all(next(candidate for candidate in tasks if candidate.key is key).status is JobStatus.SUCCEEDED for key in prior):
                continue
            tasks[index] = replace(item, status=JobStatus.READY, updated_at=at, detail="prerequisites completed")
            events.append(
                WorkflowEvent(
                    sequence=len(events) + 1,
                    task=item.key,
                    from_status=JobStatus.PENDING,
                    to_status=JobStatus.READY,
                    actor=actor,
                    reason="prerequisites completed",
                    at=at,
                )
            )
        return WorkflowState(tasks=tuple(tasks), events=tuple(events))
