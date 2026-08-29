from __future__ import annotations

from pathlib import Path

import pytest

from packages.workflow_core.agent_tasks import AgentTaskStore
from packages.workflow_core import candidate_pool as candidate_pool_module
from packages.workflow_core.candidate_pool import CandidateIdentityConflict, CandidatePoolError, CandidatePoolStore, CandidateRecord
from packages.workflow_core.locks import make_model_input_lock, make_order_policy_lock, provider_idempotency_key
from packages.workflow_core.order_policy import progressive_gates_for
from packages.workflow_core.statuses import FailureDisposition, ItemState


def _candidate(order_id: str, index: int) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=f"candidate-{index}",
        order_id=order_id,
        job_id=order_id,
        record_id=f"record-{index}",
        source="CGTrader",
        source_product_id=str(7400000 + index),
        canonical_url=f"https://www.cgtrader.com/free-3d-models/furniture/chair/item-{index}",
        preview_id=str(8400000 + index),
        preview_url=f"https://img-new.cgtrader.com/items/{index}.webp",
        capture_sha256=f"capture-{index}",
        image_sha256=f"image-{index}",
    )


def test_candidate_pool_is_durable_and_append_only(tmp_path: Path) -> None:
    order_id = "order-pool-test"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    policy = make_order_policy_lock(
        source="CGTrader", categories={"Chair": 1}, exact_n=1, provider="lux3d",
        ruleset="furniture-workflow-8.7.0", image_policy="clean-single-product",
        five_year_policy="published-within-five-years", naming_policy="deterministic-product-name.v1",
        dimension_policy="official-or-dual-agent", registry_identity="registry", registry_version="v2",
        authorization_mode="EXACT_COUNT_AUTHORIZATION", quality_policy="raw-glb-only",
    )
    pool.set_order_policy_hash(policy.order_policy_hash, target_count=1, progressive_gates=(1,))
    assert pool.add_candidates([_candidate(order_id, 1)]) == {"added": 1, "duplicates": 0, "conflicts": 0}
    assert pool.add_candidates([_candidate(order_id, 1)]) == {"added": 0, "duplicates": 1, "conflicts": 0}
    with pytest.raises(CandidateIdentityConflict):
        changed = _candidate(order_id, 1)
        changed.image_sha256 = "different"
        pool.add_candidates([changed])
    pool.mark_provider_task("candidate-1", "provider-task-1", provider="lux3d")
    pool.mark_raw_glb(
        "candidate-1", raw_glb_path="03_models/model.glb", raw_glb_sha256="a" * 64,
        valid=True, model_input_hash="b" * 64,
    )
    assert pool.success_count() == 1
    assert pool.summary()["active_provider_tasks"] == 0
    assert pool.gate_receipts() == []
    assert (tmp_path / "candidate_pool_events.jsonl").read_text(encoding="utf-8").count("\n") >= 4


def test_87_order_policy_gates_are_canonical() -> None:
    assert progressive_gates_for(300) == (1, 3, 10, 20)
    assert progressive_gates_for(3) == (1, 3)


def test_same_job_streaming_policy_migration_is_auditable(tmp_path: Path) -> None:
    order_id = "order-streaming-migration"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    old = "old-policy-hash"
    new = "new-policy-hash"
    pool.set_order_policy_hash(old, target_count=2, progressive_gates=(1, 2))
    pool.migrate_order_policy_hash(
        old, new, target_count=2, progressive_gates=(1, 2),
        reason="8.7 paused job -> 8.8 streaming policy",
    )
    assert pool.read()["order_policy_hash"] == new
    events = (tmp_path / "candidate_pool_events.jsonl").read_text(encoding="utf-8")
    assert "order_policy_migrated" in events
    with pytest.raises(CandidateIdentityConflict):
        pool.migrate_order_policy_hash(old, "another", target_count=2, progressive_gates=(1, 2), reason="bad")


def test_completed_order_retires_unsubmitted_model_input_without_provider_post(tmp_path: Path) -> None:
    order_id = "order-complete-retirement"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.add_candidates([_candidate(order_id, 1)])
    pool.transition("candidate-1", ItemState.MODEL_INPUT_LOCKED, stage_field="provider_status", stage_status="PENDING")
    retired = pool.retire_order_complete_not_needed("candidate-1")
    assert retired.state is ItemState.ORDER_COMPLETE_NOT_NEEDED
    assert retired.provider_task_id is None
    assert retired.provider_status == "NOT_SUBMITTED"
    assert pool.active_provider_count() == 0
    assert pool.available() == []
    events = (tmp_path / "candidate_pool_events.jsonl").read_text(encoding="utf-8")
    assert "candidate_order_complete_not_needed" in events


def test_frozen_scope_retires_discovered_backlog_without_provider_post(tmp_path: Path) -> None:
    order_id = "order-frozen-scope"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.add_candidates([_candidate(order_id, 1)])
    retired = pool.retire_out_of_scope("candidate-1")
    assert retired.state is ItemState.ORDER_COMPLETE_NOT_NEEDED
    assert retired.provider_task_id is None
    assert retired.provider_status == "NOT_SUBMITTED"
    assert pool.available() == []
    events = (tmp_path / "candidate_pool_events.jsonl").read_text(encoding="utf-8")
    assert "candidate_scope_retired" in events


def test_frozen_scope_rejects_active_provider_candidate(tmp_path: Path) -> None:
    order_id = "order-frozen-scope-active"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.add_candidates([_candidate(order_id, 1)])
    pool.mark_provider_task("candidate-1", "provider-task-1", provider="lux3d")
    with pytest.raises(CandidatePoolError, match="Provider activity"):
        pool.retire_out_of_scope("candidate-1")


def test_submission_unknown_is_item_quarantine_and_reserves_slot(tmp_path: Path) -> None:
    order_id = "order-submission-unknown"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.add_candidates([_candidate(order_id, 1)])
    pool.quarantine_submission_unknown("candidate-1", reason="SUBMISSION_UNKNOWN: response lost")
    record = pool.records()[0]
    assert record.state is ItemState.QUARANTINED_SUBMISSION_UNKNOWN
    assert record.provider_task_id is None
    assert pool.unresolved_submission_unknown_count() == 1
    assert pool.refill_needed(1) == 0
    assert "submission_unknown_quarantined" in (
        tmp_path / "candidate_pool_events.jsonl"
    ).read_text(encoding="utf-8")


def test_reconciled_submission_unknown_can_be_abandoned_and_never_reselected(tmp_path: Path) -> None:
    order_id = "order-submission-unknown-abandoned"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.add_candidates([_candidate(order_id, 1), _candidate(order_id, 2)])
    pool.quarantine_submission_unknown("candidate-1", reason="SUBMISSION_UNKNOWN: response lost")

    abandoned = pool.abandon_submission_unknown(
        "candidate-1", reason="final read-only reconciliation found no task ID"
    )

    assert abandoned.state is ItemState.ABANDONED_SUBMISSION_UNKNOWN
    assert abandoned.provider_task_id is None
    assert abandoned.provider_status == "ABANDONED_SUBMISSION_UNKNOWN"
    assert abandoned.lineage["submission_unknown_abandonment"]["never_resubmit"] is True
    assert abandoned.lineage["submission_unknown_abandonment"]["exact_n_reservation_released"] is True
    assert pool.unresolved_submission_unknown_count() == 0
    assert pool.refill_needed(1) == 1
    assert [item.candidate_id for item in pool.available()] == ["candidate-2"]
    events = (tmp_path / "candidate_pool_events.jsonl").read_text(encoding="utf-8")
    assert "submission_unknown_abandoned" in events


def test_submission_unknown_with_task_id_cannot_be_abandoned(tmp_path: Path) -> None:
    order_id = "order-submission-unknown-task-id"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.add_candidates([_candidate(order_id, 1)])
    pool.mark_provider_task("candidate-1", "provider-task-1", provider="lux3d")
    with pytest.raises(CandidatePoolError, match="quarantined"):
        pool.abandon_submission_unknown("candidate-1", reason="not allowed")


def test_candidate_pool_retries_transient_windows_replace_permission(tmp_path: Path, monkeypatch) -> None:
    order_id = "order-pool-retry"
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    original_replace = candidate_pool_module.os.replace
    calls = {"count": 0}

    def flaky_replace(source, destination):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError(13, "access denied")
        return original_replace(source, destination)

    monkeypatch.setattr(candidate_pool_module.os, "replace", flaky_replace)
    assert pool.add_candidates([_candidate(order_id, 1)]) == {"added": 1, "duplicates": 0, "conflicts": 0}
    assert calls["count"] == 3
    assert pool.records()[0].record_id == "record-1"


def test_agent_task_store_resumes_same_task(tmp_path: Path) -> None:
    store = AgentTaskStore(tmp_path / "agent_tasks.json", order_id="order-agent")
    task = store.create_or_get(
        candidate_id="candidate-1", task_type="visual_a", input_evidence={"image_sha256": "x"},
        local_image_path="image.webp", required_output_schema="visual.v1",
    )
    same = store.create_or_get(
        candidate_id="candidate-1", task_type="visual_a", input_evidence={"image_sha256": "x"},
        local_image_path="image.webp", required_output_schema="visual.v1",
    )
    assert task.task_id == same.task_id
    assert len(store.pending()) == 1
    store.set_receipt(task.task_id, receipt_path="receipt.json")
    assert store.pending() == []


def test_model_input_lock_and_provider_idempotency_are_deterministic() -> None:
    values = dict(
        order_id="order", candidate_id="candidate", record_id="record",
        canonical_url="https://www.cgtrader.com/item", image_sha256="a" * 64,
        visual_receipt_sha256="b" * 64, category_evidence_sha256="c" * 64,
        style="Modern", color="Black", material="Wood", product_type="Chair",
        product_name="Modern Black Wood Chair", width=1.0, depth=1.0, height=1.0,
        dimension_evidence_sha256="d" * 64, catalog_evidence_sha256="e" * 64, provider="lux3d",
    )
    first = make_model_input_lock(**values)
    second = make_model_input_lock(**values)
    assert first.model_input_hash == second.model_input_hash
    assert provider_idempotency_key(order_id="order", record_id="record", model_input_hash=first.model_input_hash, provider="lux3d") == provider_idempotency_key(order_id="order", record_id="record", model_input_hash=first.model_input_hash, provider="lux3d")
    assert FailureDisposition.REPLACE_CANDIDATE.value == "REPLACE_CANDIDATE"
    assert ItemState.COMPLETED.value == "COMPLETED"
