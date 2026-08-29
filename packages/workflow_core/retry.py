"""One retry policy for HTTP, crawler, provider, and worker recovery."""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any

from .statuses import JobStatus


@dataclass(frozen=True)
class RetryDecision:
    status: JobStatus
    retry_allowed: bool
    reason_code: str
    delay_seconds: float = 0.0


def _text(error: Any) -> str:
    return str(error or "").casefold()


def classify_failure(error: Any, *, attempt: int = 0) -> RetryDecision:
    """Classify failures without ever retrying an ambiguous submission."""

    message = _text(error)
    if isinstance(error, (ConnectionError, TimeoutError)):
        delay = backoff_seconds(attempt)
        return RetryDecision(JobStatus.RUNNING, True, "TRANSIENT_NETWORK", delay)
    if any(token in message for token in ("submission_unknown", "unknown submission", "task id unknown")):
        return RetryDecision(JobStatus.SUBMISSION_UNKNOWN, False, "SUBMISSION_UNKNOWN")
    if any(token in message for token in ("challenge", "captcha", "perimeterx", "403", "522")):
        return RetryDecision(JobStatus.PAUSED_CHALLENGE, False, "CHALLENGE_PAUSED")
    if any(token in message for token in ("generation_concurrency_limit_exceeded", "capacity", "too many tasks", "no-create")):
        return RetryDecision(JobStatus.PROVIDER_CAPACITY_WAIT, True, "PROVIDER_CAPACITY_WAIT")
    if any(token in message for token in ("timeout", "timed out", "connection", "network", "10053", "ssl", "reset")):
        delay = backoff_seconds(attempt)
        return RetryDecision(JobStatus.RUNNING, True, "TRANSIENT_NETWORK", delay)
    return RetryDecision(JobStatus.FAILED, False, "TERMINAL_FAILURE")


def backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 300.0) -> float:
    bounded = max(0, min(int(attempt), 10))
    return min(cap, base * (2 ** bounded) + random.uniform(0.0, 1.0))
