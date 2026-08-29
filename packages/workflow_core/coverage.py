"""Shared Exact-N coverage accounting for Skills and Website.

Coverage is deliberately wider than the number of completed raw files.  A
candidate that is already locked, has a known Provider task, or has a
recoverable Provider checkpoint is part of the current production boundary
and must not be replaced by a second submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .provider_safety import is_submission_unknown_marker


@dataclass(frozen=True, slots=True)
class ExactNCoverage:
    successful_raw: int = 0
    ready_unsubmitted: int = 0
    active_provider_tasks: int = 0
    recoverable_known_provider_tasks: int = 0
    unresolved_submission_unknown: int = 0

    @property
    def coverage(self) -> int:
        return (
            self.successful_raw
            + self.ready_unsubmitted
            + self.active_provider_tasks
            + self.recoverable_known_provider_tasks
            + self.unresolved_submission_unknown
        )

    def deficit(self, target: int) -> int:
        return max(0, int(target) - self.coverage)

    def to_dict(self) -> dict[str, int]:
        return {
            "successful_raw": self.successful_raw,
            "ready_unsubmitted": self.ready_unsubmitted,
            "active_provider_tasks": self.active_provider_tasks,
            "recoverable_known_provider_tasks": self.recoverable_known_provider_tasks,
            "unresolved_submission_unknown": self.unresolved_submission_unknown,
            "coverage": self.coverage,
        }


class ExactNCoverageCalculator:
    """One deterministic implementation used by both production channels."""

    @staticmethod
    def from_counts(
        *,
        successful_raw: int = 0,
        ready_unsubmitted: int = 0,
        active_provider_tasks: int = 0,
        recoverable_known_provider_tasks: int = 0,
        unresolved_submission_unknown: int = 0,
    ) -> ExactNCoverage:
        values = {
            "successful_raw": successful_raw,
            "ready_unsubmitted": ready_unsubmitted,
            "active_provider_tasks": active_provider_tasks,
            "recoverable_known_provider_tasks": recoverable_known_provider_tasks,
            "unresolved_submission_unknown": unresolved_submission_unknown,
        }
        return ExactNCoverage(**{key: max(0, int(value or 0)) for key, value in values.items()})

    @classmethod
    def from_records(
        cls, records: Iterable[Any], *, recoverable_known_provider_tasks: int = 0,
    ) -> ExactNCoverage:
        successful = ready = active = unknown = 0
        for record in records:
            state = getattr(record, "state", None)
            state_value = str(getattr(state, "value", state) or "").upper()
            raw_hash = str(getattr(record, "raw_glb_sha256", "") or "")
            task_id = str(getattr(record, "provider_task_id", "") or "")
            if is_submission_unknown_marker(
                state=state,
                provider_status=getattr(record, "provider_status", ""),
                reason=getattr(record, "rejection_reason", ""),
            ):
                unknown += 1
            elif raw_hash and state_value == "COMPLETED":
                successful += 1
            elif state_value == "MODEL_INPUT_LOCKED" and not task_id:
                ready += 1
            elif task_id and state_value in {"PROVIDER_ACTIVE", "PROVIDER_PENDING", "PROVIDER_QUEUED"}:
                active += 1
        return cls.from_counts(
            successful_raw=successful,
            ready_unsubmitted=ready,
            active_provider_tasks=active,
            recoverable_known_provider_tasks=recoverable_known_provider_tasks,
            unresolved_submission_unknown=unknown,
        )


__all__ = ["ExactNCoverage", "ExactNCoverageCalculator"]
