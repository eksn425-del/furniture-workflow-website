from __future__ import annotations

from pathlib import Path

import pytest

from furniture_workflow_engine import (
    HardStopError,
    RuntimePolicy,
    RuntimeStatus,
    SkillsWorkflowInterface,
    StageDecision,
    StageOutcome,
    WebsiteWorkflowInterface,
    SupplyExhaustedError,
)
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord
from packages.workflow_core.locks import make_order_policy_lock
from packages.workflow_core.statuses import ItemState


def _policy(order_id: str, target: int = 5) -> RuntimePolicy:
    lock = make_order_policy_lock(
        source="CGTrader", categories={"Chair": target}, exact_n=target, provider="lux3d",
        ruleset="furniture-workflow-8.7.1", image_policy="clean-single-product",
        five_year_policy="published-within-five-years", naming_policy="deterministic-product-name.v1",
        dimension_policy="official-or-dual-agent", registry_identity="registry", registry_version="v2",
        authorization_mode="EXACT_COUNT_AUTHORIZATION", quality_policy="raw-glb-only",
    )
    gates = tuple(dict.fromkeys(gate for gate in (1, 3, target) if gate <= target))
    return RuntimePolicy(
        order_id=order_id, job_id=order_id, target_count=target,
        progressive_gates=gates, provider="lux3d", order_policy=lock,
    )


def _candidate(order_id: str, index: int) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=f"candidate-{index}", order_id=order_id, job_id=order_id,
        record_id=f"record-{index}", source="CGTrader", source_product_id=str(index),
        canonical_url=f"https://www.cgtrader.com/item/{index}", preview_id=str(index),
        preview_url=f"https://img-new.cgtrader.com/item/{index}.webp",
        capture_sha256=f"capture-{index}", image_sha256=f"image-{index}",
    )


class QualificationAdapter:
    def __init__(self, visual_reject: str, provider_fail: str) -> None:
        self.visual_reject = visual_reject
        self.provider_fail = provider_fail

    def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
        if stage == "visual" and candidate.candidate_id == self.visual_reject:
            return StageOutcome(StageDecision.REJECTED, "visual quality gate rejected item")
        if stage == "submit":
            return StageOutcome(StageDecision.ACCEPTED, provider_task_id=f"task-{candidate.candidate_id}", provider="lux3d")
        if stage == "poll" and candidate.candidate_id == self.provider_fail:
            return StageOutcome(StageDecision.REJECTED, "Provider terminal failure")
        if stage == "poll":
            return StageOutcome(StageDecision.ACCEPTED, "Provider task succeeded")
        if stage == "download":
            index = candidate.candidate_id.rsplit("-", 1)[-1]
            return StageOutcome(
                StageDecision.ACCEPTED, "raw GLB downloaded", raw_glb_path=f"03_models/{candidate.candidate_id}.glb",
                raw_glb_sha256=(f"{int(index) + 1:064x}"), raw_glb_valid=True,
            )
        return StageOutcome(StageDecision.ACCEPTED, f"{stage} accepted")


def _start_pool(tmp_path: Path, order_id: str, count: int = 8, target: int = 5) -> CandidatePoolStore:
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    policy = _policy(order_id, target)
    pool.set_order_policy_hash(policy.order_policy.order_policy_hash, target_count=target, progressive_gates=policy.progressive_gates)
    pool.add_candidates([_candidate(order_id, index) for index in range(count)])
    return pool


def test_e2e_candidate_refill_finishes_exact_n_after_rejections(tmp_path: Path) -> None:
    order_id = "order-e2e"
    pool = _start_pool(tmp_path, order_id)
    pool.transition("candidate-0", ItemState.HISTORICAL_DUPLICATE, reason="permanent Registry hit")
    engine = SkillsWorkflowInterface(
        policy=_policy(order_id), pool_path=tmp_path / "candidate_pool.json",
        adapter=QualificationAdapter("candidate-1", "candidate-2"),
    )
    status = engine.run_until(max_ticks=80)
    assert status is RuntimeStatus.SUCCEEDED
    summary = engine.engine.pool.summary()
    assert summary["success_count"] == 5
    assert summary["state_counts"][ItemState.VISUAL_REJECTED.value] == 1
    assert summary["state_counts"][ItemState.PROVIDER_FAILED.value] == 1
    assert summary["state_counts"][ItemState.HISTORICAL_DUPLICATE.value] == 1
    assert [item["gate"] for item in engine.engine.pool.gate_receipts()] == [1, 3, 5]


def test_resume_keeps_known_provider_task_without_resubmission(tmp_path: Path) -> None:
    order_id = "order-resume"
    pool = _start_pool(tmp_path, order_id, count=1, target=1)

    class ResumeAdapter(QualificationAdapter):
        def __init__(self) -> None:
            super().__init__("never", "never")
            self.submit_calls = 0
            self.poll_calls = 0

        def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
            if stage == "submit":
                self.submit_calls += 1
            if stage == "poll":
                self.poll_calls += 1
                if self.poll_calls == 1:
                    return StageOutcome(StageDecision.PENDING, "provider still running")
            return super().run(stage=stage, candidate=candidate)

    first_adapter = ResumeAdapter()
    first = SkillsWorkflowInterface(policy=_policy(order_id, 1), pool_path=tmp_path / "candidate_pool.json", adapter=first_adapter)
    first.run_until(max_ticks=10)
    assert first_adapter.submit_calls == 1
    resumed = SkillsWorkflowInterface(policy=_policy(order_id, 1), pool_path=tmp_path / "candidate_pool.json", adapter=first_adapter)
    assert resumed.run_until(max_ticks=20) is RuntimeStatus.SUCCEEDED
    assert first_adapter.submit_calls == 1


def test_submission_unknown_isolated_when_provider_task_id_is_missing(tmp_path: Path) -> None:
    order_id = "order-unknown"
    _start_pool(tmp_path, order_id, count=1, target=1)

    class UnknownAdapter(QualificationAdapter):
        def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
            if stage == "submit":
                return StageOutcome(StageDecision.ACCEPTED, "provider response did not include task ID")
            return super().run(stage=stage, candidate=candidate)

    engine = SkillsWorkflowInterface(policy=_policy(order_id, 1), pool_path=tmp_path / "candidate_pool.json", adapter=UnknownAdapter("never", "never"))
    status = engine.run_until(max_ticks=20)
    assert status is RuntimeStatus.MANUAL_RECONCILIATION
    assert engine.engine.pool.read()["job_status"] == RuntimeStatus.MANUAL_RECONCILIATION.value
    assert engine.engine.pool.summary()["unresolved_submission_unknown"] == 1


def test_submission_unknown_isolated_below_threshold_and_known_items_continue(tmp_path: Path) -> None:
    order_id = "order-unknown-isolated"
    _start_pool(tmp_path, order_id, count=3, target=2)

    class UnknownFirstAdapter(QualificationAdapter):
        def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
            if stage == "submit" and candidate.candidate_id == "candidate-0":
                return StageOutcome(
                    StageDecision.HARD_STOP,
                    "SUBMISSION_UNKNOWN: provider response had no trustworthy task ID",
                )
            return super().run(stage=stage, candidate=candidate)

    engine = SkillsWorkflowInterface(
        policy=_policy(order_id, 2),
        pool_path=tmp_path / "candidate_pool.json",
        adapter=UnknownFirstAdapter("never", "never"),
    )
    status = engine.run_until(max_ticks=30)
    assert status is RuntimeStatus.MANUAL_RECONCILIATION
    summary = engine.engine.pool.summary()
    assert summary["unresolved_submission_unknown"] == 1
    assert summary["success_count"] == 1
    assert summary["state_counts"][ItemState.QUARANTINED_SUBMISSION_UNKNOWN.value] == 1


def test_submission_unknown_threshold_remains_global_hard_stop(tmp_path: Path) -> None:
    order_id = "order-unknown-threshold"
    _start_pool(tmp_path, order_id, count=5, target=4)

    class UnknownAdapter(QualificationAdapter):
        def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
            if stage == "submit":
                return StageOutcome(
                    StageDecision.HARD_STOP,
                    "SUBMISSION_UNKNOWN: provider response had no trustworthy task ID",
                )
            return super().run(stage=stage, candidate=candidate)

    engine = SkillsWorkflowInterface(
        policy=_policy(order_id, 4),
        pool_path=tmp_path / "candidate_pool.json",
        adapter=UnknownAdapter("never", "never"),
    )
    with pytest.raises(HardStopError, match="SUBMISSION_UNKNOWN_THRESHOLD"):
        engine.run_until(max_ticks=30)


def test_skills_and_website_are_two_interfaces_to_one_engine(tmp_path: Path) -> None:
    order_id = "order-interfaces"
    _start_pool(tmp_path, order_id, count=1, target=1)
    adapter = QualificationAdapter("never", "never")
    skills = SkillsWorkflowInterface(policy=_policy(order_id, 1), pool_path=tmp_path / "candidate_pool.json", adapter=adapter)
    website = WebsiteWorkflowInterface(policy=_policy(order_id, 1), pool_path=tmp_path / "candidate_pool.json", adapter=adapter)
    assert skills.engine_type is website.engine_type
    assert skills.engine_type.__name__ == "ProductionWorkflowEngine"


def test_adaptive_refill_replaces_rejected_items_until_exact_n(tmp_path: Path) -> None:
    order_id = "order-refill"
    pool = _start_pool(tmp_path, order_id, count=1, target=1)
    pool.transition("candidate-0", ItemState.VISUAL_REJECTED, reason="bad image")

    class RefillAdapter(QualificationAdapter):
        def __init__(self) -> None:
            super().__init__("never", "never")
            self.refilled = False

        def refill(self, *, needed: int, pool: CandidatePoolStore) -> list[CandidateRecord]:
            if self.refilled:
                raise SupplyExhaustedError("fixture source exhausted")
            self.refilled = True
            return [_candidate(order_id, 1)]

    adapter = RefillAdapter()
    engine = SkillsWorkflowInterface(
        policy=_policy(order_id, 1),
        pool_path=tmp_path / "candidate_pool.json",
        adapter=adapter,
    )
    assert engine.run_until(max_ticks=30) is RuntimeStatus.SUCCEEDED
    assert engine.engine.pool.summary()["metrics"]["refill_added"] == 1


def test_locked_category_quota_is_enforced_before_provider_submit(tmp_path: Path) -> None:
    order_id = "order-category-quota"
    lock = make_order_policy_lock(
        source="CGTrader", categories={"Chair": 1, "Table": 1}, exact_n=2, provider="lux3d",
        ruleset="furniture-workflow-8.7.1", image_policy="clean-single-product",
        five_year_policy="published-within-five-years", naming_policy="deterministic-product-name.v1",
        dimension_policy="official-or-dual-agent", registry_identity="registry", registry_version="v2",
        authorization_mode="EXACT_COUNT_AUTHORIZATION", quality_policy="raw-glb-only",
    )
    policy = RuntimePolicy(
        order_id=order_id, job_id=order_id, target_count=2, progressive_gates=(1, 2),
        provider="lux3d", order_policy=lock,
    )
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.set_order_policy_hash(lock.order_policy_hash, target_count=2, progressive_gates=(1, 2))
    candidates = [_candidate(order_id, index) for index in range(3)]
    candidates[0].category_group = "Chair"
    candidates[1].category_group = "Chair"
    candidates[2].category_group = "Table"
    pool.add_candidates(candidates)

    class RecordingAdapter(QualificationAdapter):
        def __init__(self) -> None:
            super().__init__("never", "never")
            self.submitted: list[str] = []

        def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
            if stage == "submit":
                self.submitted.append(candidate.candidate_id)
            return super().run(stage=stage, candidate=candidate)

    adapter = RecordingAdapter()
    engine = SkillsWorkflowInterface(policy=policy, pool_path=tmp_path / "candidate_pool.json", adapter=adapter)
    assert engine.run_until(max_ticks=40) is RuntimeStatus.SUCCEEDED
    assert len(adapter.submitted) == 2
    assert "candidate-2" in adapter.submitted


def test_completed_quota_retires_unsubmitted_lock_without_provider_post(tmp_path: Path) -> None:
    order_id = "order-quota-retire"
    lock = make_order_policy_lock(
        source="CGTrader", categories={"Chair": 2, "Table": 1}, exact_n=3, provider="lux3d",
        ruleset="furniture-workflow-8.7.1", image_policy="clean-single-product",
        five_year_policy="published-within-five-years", naming_policy="deterministic-product-name.v1",
        dimension_policy="official-or-dual-agent", registry_identity="registry", registry_version="v2",
        authorization_mode="EXACT_COUNT_AUTHORIZATION", quality_policy="raw-glb-only",
    )
    policy = RuntimePolicy(
        order_id=order_id, job_id=order_id, target_count=3, progressive_gates=(1, 3),
        provider="lux3d", order_policy=lock,
    )
    pool = CandidatePoolStore(tmp_path / "candidate_pool.json", order_id=order_id)
    pool.set_order_policy_hash(lock.order_policy_hash, target_count=3, progressive_gates=(1, 3))
    candidates = [_candidate(order_id, index) for index in range(4)]
    candidates[0].category_group = "Chair"
    candidates[1].category_group = "Chair"
    candidates[2].category_group = "Chair"
    candidates[3].category_group = "Table"
    pool.add_candidates(candidates)
    for candidate_id in ("candidate-0", "candidate-1"):
        pool.transition(candidate_id, ItemState.COMPLETED, reason="fixture")
        pool.mark_raw_glb(candidate_id, raw_glb_path=f"{candidate_id}.glb", raw_glb_sha256=(candidate_id.encode().hex() * 64)[:64], valid=True)
    pool.transition("candidate-2", ItemState.MODEL_INPUT_LOCKED, reason="fixture")
    pool.transition("candidate-3", ItemState.MODEL_INPUT_LOCKED, reason="fixture")

    class RecordingAdapter(QualificationAdapter):
        def __init__(self) -> None:
            super().__init__("never", "never")
            self.submitted: list[str] = []

        def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
            if stage == "submit":
                self.submitted.append(candidate.candidate_id)
            return super().run(stage=stage, candidate=candidate)

    adapter = RecordingAdapter()
    engine = SkillsWorkflowInterface(policy=policy, pool_path=tmp_path / "candidate_pool.json", adapter=adapter)
    engine.engine.tick()
    retired = engine.engine.pool.read()["items"]["candidate-2"]
    assert retired["state"] == ItemState.CATEGORY_REJECTED.value
    assert retired["rejection_reason"] == "QUOTA_ALREADY_FILLED"
    assert "candidate-2" not in adapter.submitted
