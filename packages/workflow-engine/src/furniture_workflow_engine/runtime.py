"""The single production Workflow Engine used by Skills and Website.

This module owns per-item progression, ordinary rejection isolation, adaptive
refill accounting, progressive live gates, Exact-N, and hard-stop semantics.
Site/provider workers are adapters supplied by an interface; they are not
allowed to invent a second control loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord, SubmissionUnknown
from packages.workflow_core.locks import OrderPolicyLock
from packages.workflow_core.provider_safety import MAX_UNRESOLVED_SUBMISSIONS
from packages.workflow_core.statuses import FailureDisposition, ItemState


ENGINE_SCHEMA_VERSION = "workflow-engine.v8.8.1"
PROGRESSIVE_GATE_CHAIN = [
    "source_discovery",
    "historical_registry_dedup",
    "canonical_capture",
    "media_resolver",
    "image_quality_gate",
    "host_agent_semantic_receipt",
    "catalog_lock",
    "deterministic_naming",
    "dimension_resolution",
    "provider_submit",
    "provider_task_reconcile",
    "raw_glb_download_validation",
]


class RuntimeStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_REFILL = "WAITING_REFILL"
    WAITING_AGENT = "WAITING_AGENT"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    SUPPLY_EXHAUSTED = "SUPPLY_EXHAUSTED"
    SUCCEEDED = "SUCCEEDED"
    HARD_STOP = "HARD_STOP"
    QUALIFICATION_REPAIR = "QUALIFICATION_REPAIR"
    QUALIFICATION_PASSED = "QUALIFICATION_PASSED"
    MANUAL_RECONCILIATION = "MANUAL_RECONCILIATION"


class HardStopError(RuntimeError):
    """A condition where continuing could duplicate charge or corrupt lineage."""

    def __init__(self, code: str, message: str, *, candidate_id: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.candidate_id = candidate_id


class SupplyExhaustedError(RuntimeError):
    """The source adapter has truthfully exhausted replacement candidates."""


class StageDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"
    RETRY = "retry"
    HARD_STOP = "hard_stop"
    SOFTWARE_AUTO_REPAIR = "software_auto_repair"


@dataclass(frozen=True)
class StageOutcome:
    decision: StageDecision
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    provider_task_id: str | None = None
    provider: str | None = None
    raw_glb_path: str | None = None
    raw_glb_sha256: str | None = None
    raw_glb_valid: bool = False


class StageAdapter(Protocol):
    def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome: ...

    def refill(self, *, needed: int, pool: CandidatePoolStore) -> list[CandidateRecord] | None: ...


@dataclass(frozen=True)
class RuntimePolicy:
    order_id: str
    job_id: str
    target_count: int
    progressive_gates: tuple[int, ...]
    provider: str
    order_policy: OrderPolicyLock
    max_provider_slots: int = 5
    max_steps_per_tick: int = 20
    max_refill_rounds: int = 100
    # Legacy direct engine callers did not pass a spillover policy and relied
    # on the historical bounded spillover behavior.  Website contracts always
    # pass their explicit UI choice (ASK/STOP/AUTO_IF_EXPLICIT).
    spillover: str = "AUTO_IF_EXPLICIT"

    def __post_init__(self) -> None:
        gates = tuple(int(value) for value in self.progressive_gates)
        if gates != tuple(sorted(set(gates))):
            raise ValueError("progressive gates must be strictly increasing")
        if any(gate < 1 or gate > self.target_count for gate in gates):
            raise ValueError("progressive gate is outside the Exact-N target")
        if self.target_count != self.order_policy.exact_n:
            raise ValueError("runtime target must equal locked Exact-N")
        if not 1 <= int(self.max_provider_slots) <= 5:
            raise ValueError("max_provider_slots must be between 1 and 5")


def next_stage_for(state: ItemState) -> str | None:
    return {
        ItemState.DISCOVERED: "capture",
        ItemState.CAPTURE_READY: "media",
        ItemState.MEDIA_READY: "visual",
        ItemState.VISUAL_PENDING: "visual",
        ItemState.VISUAL_ACCEPTED: "category",
        ItemState.CATEGORY_ACCEPTED: "date",
        ItemState.DATE_ACCEPTED: "dimension",
        ItemState.DIMENSION_PENDING: "dimension",
        ItemState.DIMENSION_READY: "naming",
        ItemState.NAMING_READY: "catalog",
        ItemState.CATALOG_READY: "model_input",
        ItemState.MODEL_INPUT_LOCKED: "submit",
        ItemState.PROVIDER_ACTIVE: "poll",
        ItemState.PROVIDER_SUCCESS: "download",
    }.get(state)


def _stage_field(stage: str) -> str | None:
    return {
        "capture": "historical_status",
        "media": "media_status",
        "visual": "visual_status",
        "category": "category_status",
        "date": "date_status",
        "dimension": "dimension_status",
        "catalog": "catalog_status",
        "submit": "provider_status",
        "poll": "provider_status",
        "download": "provider_status",
    }.get(stage)


def _accepted_state(stage: str) -> ItemState:
    return {
        "capture": ItemState.CAPTURE_READY,
        "media": ItemState.MEDIA_READY,
        "visual": ItemState.VISUAL_ACCEPTED,
        "category": ItemState.CATEGORY_ACCEPTED,
        "date": ItemState.DATE_ACCEPTED,
        "dimension": ItemState.DIMENSION_READY,
        "naming": ItemState.NAMING_READY,
        "catalog": ItemState.CATALOG_READY,
        "model_input": ItemState.MODEL_INPUT_LOCKED,
        "poll": ItemState.PROVIDER_SUCCESS,
    }.get(stage, ItemState.DISCOVERED)


def _rejected_state(stage: str) -> ItemState:
    return {
        "capture": ItemState.CAPTURE_REJECTED,
        "media": ItemState.MEDIA_REJECTED,
        "visual": ItemState.VISUAL_REJECTED,
        "category": ItemState.CATEGORY_REJECTED,
        "date": ItemState.DATE_REJECTED,
        "dimension": ItemState.DIMENSION_REJECTED,
        "naming": ItemState.NAMING_REVIEW,
        "submit": ItemState.PROVIDER_FAILED,
        "poll": ItemState.PROVIDER_FAILED,
        "download": ItemState.RAW_GLB_INVALID,
    }.get(stage, ItemState.MANUAL_REVIEW)


class ProductionWorkflowEngine:
    """Single bounded control loop. Interface differences live outside it."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        pool: CandidatePoolStore,
        adapter: StageAdapter,
        completion_recorder=None,
    ) -> None:
        self.policy = policy
        self.pool = pool
        self.adapter = adapter
        self.completion_recorder = completion_recorder
        self.refill_rounds = 0
        self.pool.set_order_policy_hash(
            policy.order_policy.order_policy_hash,
            target_count=policy.target_count,
            progressive_gates=policy.progressive_gates,
        )

    def _category_key(self, candidate: CandidateRecord) -> str:
        # Production quota locks are keyed by immutable taxonomy scope IDs.
        # Keep the display group as a compatibility fallback for older test
        # fixtures and legacy pools that predate scope IDs.
        stable_scope = str(candidate.lineage.get("category_id") or "").strip()
        if stable_scope and stable_scope in self.policy.order_policy.categories:
            return stable_scope
        value = str(candidate.category_group or "").strip()
        if value:
            return value
        categories = self.policy.order_policy.categories
        # Compatibility fixtures from 8.6 did not carry category_group.  A
        # single-category policy can safely infer it; multi-category
        # production orders must provide the locked group explicitly.
        if len(categories) == 1:
            return next(iter(categories))
        return ""

    def _category_outstanding(self, category: str) -> int:
        return sum(
            1
            for item in self.pool.records()
            if self._category_key(item) == category
            and item.state in {ItemState.COMPLETED, ItemState.PROVIDER_ACTIVE, ItemState.PROVIDER_SUCCESS}
        )

    def _category_completed(self, category: str) -> int:
        return sum(
            1
            for item in self.pool.records()
            if self._category_key(item) == category
            and item.state is ItemState.COMPLETED
            and item.raw_glb_sha256
        )

    def _category_has_remaining_supply(self, category: str) -> bool:
        return any(
            self._category_key(item) == category and next_stage_for(item.state) is not None
            for item in self.pool.records()
        )

    def _spillover_deficit(self) -> int:
        """硬性均分下，某类目因缺货而无法填满其份额的缺口总量。

        只有当某类目「仍未达到其份额」且「已没有任何还能继续推进的在途候选」时，
        才认为它缺货。这个缺口允许由其他超额类目溢出填补，避免因单个类目缺货
        而让整批卡死在 BOUNDED_TICK_LIMIT。
        """
        categories = self.policy.order_policy.categories or {}
        deficit = 0
        for category, quota in categories.items():
            q = int(quota)
            outstanding = self._category_outstanding(category)
            if outstanding >= q:
                continue
            if self._category_has_remaining_supply(category):
                continue
            deficit += q - outstanding
        return deficit

    def _category_has_capacity(self, candidate: CandidateRecord) -> bool:
        if getattr(self.policy.order_policy, "category_quota_mode", "REQUIRED") == "NONE":
            return True
        category = self._category_key(candidate)
        quota = self.policy.order_policy.categories.get(category)
        if quota is None:
            # A REQUIRED quota lock is also a scope lock.  Candidates whose
            # category is not one of the approved quota keys must never consume
            # the global target or a spillover deficit.
            return False
        if self._category_outstanding(category) < int(quota):
            return True
        # 本类目份额已满：仅当其他类目因缺货存在未填满的缺口时才允许溢出，
        # 否则保持硬性份额（防止一个类目把总目标全部吃掉）。
        return str(self.policy.spillover or "ASK").upper() == "AUTO_IF_EXPLICIT" and self._spillover_deficit() > 0

    def _retire_overquota_locks(self) -> int:
        if getattr(self.policy.order_policy, "category_quota_mode", "REQUIRED") == "NONE":
            return 0
        # 存在缺货缺口时，超额类目的在途候选正是用来溢出填补缺口的，
        # 此时不回收，避免把可用的溢出候选提前清掉。
        if str(self.policy.spillover or "ASK").upper() == "AUTO_IF_EXPLICIT" and self._spillover_deficit() > 0:
            return 0
        retired = 0
        for candidate in self.pool.records():
            if candidate.state is not ItemState.MODEL_INPUT_LOCKED:
                continue
            category = self._category_key(candidate)
            quota = self.policy.order_policy.categories.get(category)
            if quota is None or self._category_completed(category) < int(quota):
                continue
            self.pool.retire_quota_locked(candidate.candidate_id, category=category)
            retired += 1
        return retired

    def tick(self) -> RuntimeStatus:
        if self.pool.success_count() >= self.policy.target_count:
            self.pool.set_job_status(RuntimeStatus.SUCCEEDED.value, reason="Exact-N raw GLB count reached")
            return RuntimeStatus.SUCCEEDED
        steps = 0
        progressed = False
        self._retire_overquota_locks()
        candidates = [
            item for item in self.pool.records()
            if next_stage_for(item.state) is not None
        ]
        for candidate in sorted(candidates, key=lambda item: (item.created_at, item.candidate_id)):
            if steps >= self.policy.max_steps_per_tick:
                break
            stage = next_stage_for(candidate.state)
            if stage is None:
                continue
            if stage == "submit":
                if not self._category_has_capacity(candidate):
                    continue
                outstanding_provider = (
                    self.pool.active_provider_count()
                    + self.pool.unresolved_submission_unknown_count()
                    + sum(
                        1 for item in self.pool.records() if item.state is ItemState.PROVIDER_SUCCESS
                    )
                )
                if outstanding_provider >= self.policy.max_provider_slots:
                    continue
                if self.pool.success_count() + outstanding_provider >= self.policy.target_count:
                    continue
            outcome = self.adapter.run(stage=stage, candidate=candidate)
            steps += 1
            if self._apply(candidate, stage, outcome):
                progressed = True
            if self.pool.success_count() >= self.policy.target_count:
                self.pool.set_job_status(RuntimeStatus.SUCCEEDED.value, reason="Exact-N raw GLB count reached")
                return RuntimeStatus.SUCCEEDED

        if self.pool.active_provider_count():
            self.pool.set_job_status(RuntimeStatus.WAITING_PROVIDER.value, reason="known Provider tasks remain active")
            return RuntimeStatus.WAITING_PROVIDER
        unresolved = self.pool.unresolved_submission_unknown_count()
        provider_success_pending = any(
            item.state is ItemState.PROVIDER_SUCCESS for item in self.pool.records()
        )
        if (
            unresolved
            and self.pool.success_count() + unresolved >= self.policy.target_count
            and not provider_success_pending
        ):
            # An unresolved paid submission reserves its Exact-N slot. Once the
            # confirmed count plus that reservation reaches the target, no new
            # POST is safe. Stop at an explicit manual-reconciliation boundary
            # instead of spinning forever on unsubmitted MODEL_INPUT_LOCKED rows.
            reason = (
                f"Exact-N boundary reached with {self.pool.success_count()} confirmed raw GLB(s) "
                f"and {unresolved} unresolved submission_unknown reservation(s); "
                "reconcile the Provider before any replacement submission"
            )
            self.pool.set_job_status(RuntimeStatus.MANUAL_RECONCILIATION.value, reason=reason)
            return RuntimeStatus.MANUAL_RECONCILIATION
        capacity_wait = any(
            item.state is ItemState.MODEL_INPUT_LOCKED
            and str(item.provider_status or "").upper() == "CAPACITY_WAIT"
            for item in self.pool.records()
        )
        if not progressed and capacity_wait:
            self.pool.set_job_status(
                RuntimeStatus.WAITING_PROVIDER.value,
                reason="Provider capacity rejected creation; no new task was posted",
            )
            return RuntimeStatus.WAITING_PROVIDER
        if any(next_stage_for(item.state) is not None for item in self.pool.records()):
            self.pool.set_job_status(RuntimeStatus.RUNNING.value, reason="candidate stages remain")
            return RuntimeStatus.RUNNING
        refill = getattr(self.adapter, "refill", None)
        if callable(refill):
            if self.refill_rounds >= self.policy.max_refill_rounds:
                return self.declare_supply_exhausted(reason="bounded candidate refill rounds exhausted")
            needed = self.pool.refill_needed(self.policy.target_count)
            if needed > 0:
                self.refill_rounds += 1
                try:
                    values = list(refill(needed=needed, pool=self.pool) or [])
                except SupplyExhaustedError as error:
                    return self.declare_supply_exhausted(reason=str(error))
                except Exception as error:
                    if error.__class__.__name__ == "AgentReviewPending":
                        self.pool.set_job_status(RuntimeStatus.WAITING_AGENT.value, reason=str(error))
                        return RuntimeStatus.WAITING_AGENT
                    raise
                result = self.pool.add_candidates(values)
                self.pool.record_refill(requested=needed, added=result["added"], reason="adaptive replacement supply")
                if result["added"]:
                    self.pool.set_job_status(RuntimeStatus.RUNNING.value, reason="replacement candidates added")
                    return RuntimeStatus.RUNNING
                return self.declare_supply_exhausted(reason="candidate source returned no new candidates")
        if not any(next_stage_for(item.state) is not None for item in self.pool.records()):
            return self.declare_supply_exhausted(reason="candidate source has no remaining replacement candidates")
        self.pool.set_job_status(
            RuntimeStatus.WAITING_REFILL.value,
            reason="ordinary item rejection requires bounded refill" if progressed else "candidate pool needs more source supply",
        )
        return RuntimeStatus.WAITING_REFILL

    def run_until(self, *, max_ticks: int = 100, sleep_seconds: float = 0.0) -> RuntimeStatus:
        for _ in range(max_ticks):
            status = self.tick()
            if status in {
                RuntimeStatus.SUCCEEDED,
                RuntimeStatus.HARD_STOP,
                RuntimeStatus.SUPPLY_EXHAUSTED,
                RuntimeStatus.WAITING_AGENT,
                RuntimeStatus.QUALIFICATION_REPAIR,
                RuntimeStatus.MANUAL_RECONCILIATION,
            }:
                return status
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        return RuntimeStatus(self.pool.read().get("job_status") or RuntimeStatus.WAITING_REFILL.value)

    def declare_supply_exhausted(self, *, reason: str) -> RuntimeStatus:
        if self.pool.success_count() >= self.policy.target_count:
            return RuntimeStatus.SUCCEEDED
        if self.pool.active_provider_count():
            return RuntimeStatus.WAITING_PROVIDER
        self.pool.set_job_status(RuntimeStatus.SUPPLY_EXHAUSTED.value, reason=reason)
        return RuntimeStatus.SUPPLY_EXHAUSTED

    def _apply(self, candidate: CandidateRecord, stage: str, outcome: StageOutcome) -> bool:
        decision = StageDecision(outcome.decision)
        if decision is StageDecision.HARD_STOP:
            if "SUBMISSION_UNKNOWN" in str(outcome.reason or "").upper():
                self.pool.quarantine_submission_unknown(
                    candidate.candidate_id,
                    reason=outcome.reason or "submission_unknown",
                )
                unresolved = self.pool.unresolved_submission_unknown_count()
                if unresolved > MAX_UNRESOLVED_SUBMISSIONS:
                    self.pool.set_job_status(
                        RuntimeStatus.HARD_STOP.value,
                        reason=(
                            f"unresolved submission_unknown count {unresolved} exceeds "
                            f"safe threshold {MAX_UNRESOLVED_SUBMISSIONS}"
                        ),
                    )
                    raise HardStopError(
                        "SUBMISSION_UNKNOWN_THRESHOLD",
                        "too many unresolved Provider submissions",
                        candidate_id=candidate.candidate_id,
                    )
                self.pool.set_job_status(
                    RuntimeStatus.RUNNING.value,
                    reason=(
                        f"isolated submission_unknown; {unresolved} slot reservation(s) remain quarantined"
                    ),
                )
                return True
            self.pool.transition(
                candidate.candidate_id,
                ItemState.HARD_STOP_ITEM,
                reason=outcome.reason or "hard stop",
                disposition=FailureDisposition.HARD_STOP,
            )
            self.pool.set_job_status(RuntimeStatus.HARD_STOP.value, reason=outcome.reason)
            raise HardStopError(outcome.reason or "HARD_STOP", outcome.reason or "workflow hard stop", candidate_id=candidate.candidate_id)
        if decision is StageDecision.SOFTWARE_AUTO_REPAIR:
            self.pool.transition(
                candidate.candidate_id,
                candidate.state,
                reason=outcome.reason,
                disposition=FailureDisposition.SOFTWARE_AUTO_REPAIR,
                retry=True,
            )
            self.pool.set_job_status(RuntimeStatus.QUALIFICATION_REPAIR.value, reason=outcome.reason)
            return False
        if decision is StageDecision.RETRY:
            self.pool.transition(
                candidate.candidate_id,
                candidate.state,
                stage_field=_stage_field(stage),
                stage_status="RETRY",
                reason=outcome.reason,
                disposition=FailureDisposition.RETRY_ITEM,
                retry=True,
            )
            return False
        if decision is StageDecision.PENDING:
            pending_state = ItemState.VISUAL_PENDING if stage == "visual" else ItemState.DIMENSION_PENDING if stage == "dimension" else candidate.state
            pending_status = (
                "CAPACITY_WAIT"
                if stage == "submit" and any(token in outcome.reason.casefold() for token in ("capacity", "busy", "slot", "limit"))
                else "PENDING"
            )
            self.pool.transition(
                candidate.candidate_id,
                pending_state,
                stage_field=_stage_field(stage),
                stage_status=pending_status,
                reason=outcome.reason,
                disposition=FailureDisposition.WAIT_PROVIDER,
            )
            return False
        if decision is StageDecision.REJECTED:
            self.pool.transition(
                candidate.candidate_id,
                _rejected_state(stage),
                stage_field=_stage_field(stage),
                stage_status="REJECTED",
                reason=outcome.reason or "stage rejected candidate",
                disposition=FailureDisposition.REPLACE_CANDIDATE,
            )
            return True
        if stage == "submit":
            try:
                self.pool.mark_provider_task(
                    candidate.candidate_id,
                    outcome.provider_task_id,
                    provider=outcome.provider or self.policy.provider,
                )
            except SubmissionUnknown as error:
                unresolved = self.pool.unresolved_submission_unknown_count()
                if unresolved > MAX_UNRESOLVED_SUBMISSIONS:
                    self.pool.set_job_status(
                        RuntimeStatus.HARD_STOP.value,
                        reason=f"unresolved submission_unknown count {unresolved} exceeds safe threshold {MAX_UNRESOLVED_SUBMISSIONS}",
                    )
                    raise HardStopError(
                        "SUBMISSION_UNKNOWN_THRESHOLD",
                        "too many unresolved Provider submissions",
                        candidate_id=candidate.candidate_id,
                    ) from error
                self.pool.set_job_status(
                    RuntimeStatus.RUNNING.value,
                    reason=f"isolated submission_unknown; {unresolved} slot reservation(s) remain quarantined",
                )
                return True
            return True
        if stage == "download":
            if not outcome.raw_glb_path or not outcome.raw_glb_sha256:
                self.pool.transition(
                    candidate.candidate_id,
                    ItemState.RAW_GLB_INVALID,
                    stage_field="provider_status",
                    stage_status="FAILED",
                    reason="download outcome did not include raw GLB hash/path",
                    disposition=FailureDisposition.REPLACE_CANDIDATE,
                )
                return True
            record = self.pool.mark_raw_glb(
                candidate.candidate_id,
                raw_glb_path=outcome.raw_glb_path,
                raw_glb_sha256=outcome.raw_glb_sha256,
                valid=outcome.raw_glb_valid,
                lineage=dict(outcome.evidence),
            )
            if record.state is ItemState.COMPLETED:
                if self.completion_recorder is not None:
                    try:
                        self.completion_recorder(record)
                    except Exception as error:
                        self.pool.set_job_status(
                            RuntimeStatus.HARD_STOP.value,
                            reason=f"permanent Registry write failed: {error}",
                        )
                        raise HardStopError(
                            "REGISTRY_WRITE_FAILED",
                            "permanent Registry/event ledger could not be updated",
                            candidate_id=record.candidate_id,
                        ) from error
                self._record_reached_gates()
            return True
        self.pool.transition(
            candidate.candidate_id,
            _accepted_state(stage),
            stage_field=_stage_field(stage),
            stage_status="READY",
            reason=outcome.reason or f"{stage} accepted",
            lineage=dict(outcome.evidence),
        )
        return True

    def _record_reached_gates(self) -> None:
        reached = {int(item.get("gate") or 0) for item in self.pool.gate_receipts() if item.get("status") == "PASS"}
        for gate in self.policy.progressive_gates:
            if gate in reached or self.pool.success_count() < gate:
                continue
            self.pool.record_gate(gate, chain=PROGRESSIVE_GATE_CHAIN)


class WorkflowInterface:
    """Thin adapter base; no interface is allowed to own workflow policy."""

    engine_type = ProductionWorkflowEngine

    def __init__(self, *, policy: RuntimePolicy, pool_path: Path, adapter: StageAdapter) -> None:
        self.policy = policy
        self.engine = ProductionWorkflowEngine(
            policy=policy,
            pool=CandidatePoolStore(pool_path, order_id=policy.order_id, job_id=policy.job_id),
            adapter=adapter,
        )

    def tick(self) -> RuntimeStatus:
        return self.engine.tick()

    def run_until(self, *, max_ticks: int = 100) -> RuntimeStatus:
        return self.engine.run_until(max_ticks=max_ticks)


class SkillsWorkflowInterface(WorkflowInterface):
    interface_name = "skills"


class WebsiteWorkflowInterface(WorkflowInterface):
    interface_name = "website"


__all__ = [
    "ENGINE_SCHEMA_VERSION",
    "HardStopError",
    "PROGRESSIVE_GATE_CHAIN",
    "ProductionWorkflowEngine",
    "RuntimePolicy",
    "RuntimeStatus",
    "SupplyExhaustedError",
    "SkillsWorkflowInterface",
    "StageAdapter",
    "StageDecision",
    "StageOutcome",
    "WebsiteWorkflowInterface",
    "WorkflowInterface",
]
