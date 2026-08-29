"""Exact-N candidate accounting and deterministic refill decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


class CandidateState(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass
class Candidate:
    record_id: str
    identity_key: str
    source_sha256: str
    name: str = ""
    state: CandidateState = CandidateState.AVAILABLE
    provider_task_id: str | None = None
    reason: str = ""


@dataclass
class ExactNPlan:
    requested: int
    candidates: dict[str, Candidate] = field(default_factory=dict)
    succeeded_ids: set[str] = field(default_factory=set)

    def add_candidates(self, values: Iterable[Candidate]) -> int:
        added = 0
        existing_identity = {item.identity_key for item in self.candidates.values()}
        for candidate in values:
            if not candidate.record_id or not candidate.identity_key or not candidate.source_sha256:
                continue
            if candidate.record_id in self.candidates or candidate.identity_key in existing_identity:
                candidate.state = CandidateState.DUPLICATE
                candidate.reason = "duplicate_record_or_identity"
                continue
            self.candidates[candidate.record_id] = candidate
            existing_identity.add(candidate.identity_key)
            added += 1
        return added

    @property
    def succeeded(self) -> int:
        return len(self.succeeded_ids)

    @property
    def remaining(self) -> int:
        return max(0, self.requested - self.succeeded)

    @property
    def available(self) -> list[Candidate]:
        return sorted(
            (item for item in self.candidates.values() if item.state == CandidateState.AVAILABLE),
            key=lambda item: item.record_id,
        )

    def reserve(self, count: int) -> list[Candidate]:
        if count <= 0 or self.remaining <= 0:
            return []
        selected = self.available[: min(count, self.remaining)]
        for candidate in selected:
            candidate.state = CandidateState.RESERVED
        return selected

    def mark_submitted(self, record_id: str, provider_task_id: str) -> None:
        candidate = self.candidates[record_id]
        candidate.state = CandidateState.SUBMITTED
        candidate.provider_task_id = provider_task_id

    def mark_succeeded(self, record_id: str) -> None:
        candidate = self.candidates[record_id]
        candidate.state = CandidateState.SUCCEEDED
        self.succeeded_ids.add(record_id)

    def mark_rejected(self, record_id: str, reason: str) -> None:
        candidate = self.candidates[record_id]
        candidate.state = CandidateState.REJECTED
        candidate.reason = reason

    def terminal_status(self) -> str:
        if self.succeeded >= self.requested:
            return "SUCCEEDED"
        if not self.available and not any(
            item.state in {CandidateState.RESERVED, CandidateState.SUBMITTED, CandidateState.MANUAL_REVIEW}
            for item in self.candidates.values()
        ):
            return "SUPPLY_EXHAUSTED"
        if self.remaining > 0 and not self.available:
            return "WAITING_PROVIDER"
        return "WAITING_REFILL" if self.remaining > 0 else "RUNNING"
