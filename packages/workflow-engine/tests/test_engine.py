from __future__ import annotations

import pytest

from furniture_workflow_engine import (
    BusinessStage,
    GateContext,
    GateViolation,
    InvalidTransition,
    JobStatus,
    TaskKey,
    WorkflowEngine,
)


def succeed(engine: WorkflowEngine, state, task: TaskKey, gates: GateContext | None = None):
    state = engine.transition(state, task, JobStatus.RUNNING, actor="test", reason="start", gates=gates)
    return engine.transition(state, task, JobStatus.SUCCEEDED, actor="test", reason="done")


def complete_until(engine: WorkflowEngine, state, stop_before: TaskKey):
    for task in TaskKey:
        if task is stop_before:
            return state
        gates = None
        if task is TaskKey.MODEL_GENERATION:
            gates = GateContext(catalog_lock_hash="lock", paid_generation_approval_hash="lock")
        elif task is TaskKey.BLENDER_EXPORT:
            gates = GateContext(catalog_lock_hash="lock", export_approval_hash="lock")
        elif task is TaskKey.ENTERPRISE_INPUT:
            gates = GateContext(enterprise_input_present=True)
        state = succeed(engine, state, task, gates)
    raise AssertionError("stop task was not found")


def test_new_workflow_only_unlocks_ingest():
    state = WorkflowEngine().new()
    assert state.task(TaskKey.PROJECT_INGEST).status is JobStatus.READY
    assert all(state.task(key).status is JobStatus.PENDING for key in list(TaskKey)[1:])


def test_cannot_skip_prerequisites():
    engine = WorkflowEngine()
    with pytest.raises((InvalidTransition, GateViolation)):
        engine.transition(
            engine.new(),
            TaskKey.PRODUCT_DISCOVERY,
            JobStatus.READY,
            actor="test",
            reason="skip",
        )


def test_success_unlocks_the_next_task_and_records_events():
    engine = WorkflowEngine()
    state = succeed(engine, engine.new(), TaskKey.PROJECT_INGEST)
    assert state.task(TaskKey.PRODUCT_DISCOVERY).status is JobStatus.READY
    assert state.task(TaskKey.ASSET_ACQUISITION).status is JobStatus.PENDING
    assert state.events[-1].reason == "prerequisites completed"


def test_review_requires_an_explicit_retry():
    engine = WorkflowEngine()
    state = engine.new()
    state = engine.transition(state, TaskKey.PROJECT_INGEST, JobStatus.RUNNING, actor="worker", reason="start")
    state = engine.transition(
        state,
        TaskKey.PROJECT_INGEST,
        JobStatus.NEEDS_REVIEW,
        actor="worker",
        reason="ambiguous input",
    )
    state = engine.transition(state, TaskKey.PROJECT_INGEST, JobStatus.READY, actor="reviewer", reason="approved retry")
    assert state.task(TaskKey.PROJECT_INGEST).status is JobStatus.READY


def test_paid_generation_requires_matching_catalog_approval():
    engine = WorkflowEngine()
    state = complete_until(engine, engine.new(), TaskKey.MODEL_GENERATION)
    with pytest.raises(GateViolation):
        engine.transition(
            state,
            TaskKey.MODEL_GENERATION,
            JobStatus.RUNNING,
            actor="operator",
            reason="start batch",
            gates=GateContext(catalog_lock_hash="lock", paid_generation_approval_hash="other"),
        )
    state = engine.transition(
        state,
        TaskKey.MODEL_GENERATION,
        JobStatus.RUNNING,
        actor="operator",
        reason="start approved batch",
        gates=GateContext(catalog_lock_hash="lock", paid_generation_approval_hash="lock"),
    )
    assert state.task(TaskKey.MODEL_GENERATION).attempt == 1


def test_enterprise_input_stage_requires_exported_table():
    engine = WorkflowEngine()
    state = complete_until(engine, engine.new(), TaskKey.ENTERPRISE_INPUT)
    with pytest.raises(GateViolation):
        engine.transition(
            state,
            TaskKey.ENTERPRISE_INPUT,
            JobStatus.RUNNING,
            actor="operator",
            reason="receive export",
        )


def test_glb_validation_has_a_separate_export_stage_before_input_and_qa():
    engine = WorkflowEngine()
    state = complete_until(engine, engine.new(), TaskKey.ENTERPRISE_INPUT)
    models = next(view for view in engine.stage_views(state) if view.key is BusinessStage.MODELS)
    export = next(view for view in engine.stage_views(state) if view.key is BusinessStage.EXPORT)
    input_stage = next(view for view in engine.stage_views(state) if view.key is BusinessStage.INPUT)
    qa = next(view for view in engine.stage_views(state) if view.key is BusinessStage.QA)
    assert models.status is JobStatus.SUCCEEDED
    assert export.status is JobStatus.SUCCEEDED
    assert input_stage.status is JobStatus.READY
    assert qa.status is JobStatus.PENDING


def test_stage_view_aggregates_internal_tasks():
    engine = WorkflowEngine()
    state = succeed(engine, engine.new(), TaskKey.PROJECT_INGEST)
    scrape = next(view for view in engine.stage_views(state) if view.key is BusinessStage.SCRAPE)
    repair = next(view for view in engine.stage_views(state) if view.key is BusinessStage.REPAIR)
    assert scrape.status is JobStatus.READY
    assert scrape.completed_tasks == 1
    assert scrape.total_tasks == 3
    assert repair.status is JobStatus.PENDING


def test_succeeded_task_is_immutable():
    engine = WorkflowEngine()
    state = succeed(engine, engine.new(), TaskKey.PROJECT_INGEST)
    with pytest.raises(InvalidTransition):
        engine.transition(
            state,
            TaskKey.PROJECT_INGEST,
            JobStatus.READY,
            actor="test",
            reason="reopen",
        )


def test_workflow_state_round_trips_without_losing_events():
    engine = WorkflowEngine()
    state = succeed(engine, engine.new(actor="api"), TaskKey.PROJECT_INGEST)
    restored = type(state).from_dict(state.to_dict())
    assert restored == state
    assert restored.events[-1].sequence == len(restored.events)


def test_workflow_state_rejects_duplicate_or_missing_tasks():
    state = WorkflowEngine().new().to_dict()
    state["tasks"] = state["tasks"][:-1] + [state["tasks"][0]]
    with pytest.raises(ValueError, match="every task|duplicate"):
        from furniture_workflow_engine import WorkflowState

        WorkflowState.from_dict(state)
