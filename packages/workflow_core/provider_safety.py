"""Shared safety rules for ambiguous paid-provider submissions."""

from __future__ import annotations

MAX_UNRESOLVED_SUBMISSIONS = 3


def is_submission_unknown_marker(*, state: object, provider_status: object = "", reason: object = "") -> bool:
    """Recognize both the current quarantine state and older ledger markings."""

    state_value = str(getattr(state, "value", state) or "").upper()
    provider_value = str(provider_status or "").upper()
    reason_value = str(reason or "").upper()
    return (
        state_value == "QUARANTINED_SUBMISSION_UNKNOWN"
        or (
            state_value == "HARD_STOP_ITEM"
            and (provider_value == "SUBMISSION_UNKNOWN" or "SUBMISSION_UNKNOWN" in reason_value)
        )
    )


__all__ = ["MAX_UNRESOLVED_SUBMISSIONS", "is_submission_unknown_marker"]
