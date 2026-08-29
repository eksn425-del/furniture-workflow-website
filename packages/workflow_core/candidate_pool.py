"""Durable Exact-N candidate pool shared by Skills and Website.

The pool is deliberately filesystem-backed so a portable Skills process and
the Website workers can resume the same order without copying or translating
state.  Writes are atomic and every mutation appends an event to the local
event ledger.  The permanent CGTrader Registry remains a separate append-only
authority; this module never deletes or rewrites it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Iterator

from .statuses import FailureDisposition, ItemState
from .provider_safety import is_submission_unknown_marker


POOL_SCHEMA_VERSION = "candidate-pool.v2"
POOL_EVENT_SCHEMA_VERSION = "candidate-pool-event.v1"
TERMINAL_ITEM_STATES = {
    ItemState.HISTORICAL_DUPLICATE,
    ItemState.CAPTURE_REJECTED,
    ItemState.MEDIA_REJECTED,
    ItemState.VISUAL_REJECTED,
    ItemState.CATEGORY_REJECTED,
    ItemState.DATE_REJECTED,
    ItemState.DIMENSION_REJECTED,
    ItemState.NAMING_REVIEW,
    ItemState.PROVIDER_FAILED,
    ItemState.RAW_GLB_INVALID,
    ItemState.MANUAL_REVIEW,
    ItemState.QUARANTINED_SUBMISSION_UNKNOWN,
    ItemState.ABANDONED_SUBMISSION_UNKNOWN,
    ItemState.HARD_STOP_ITEM,
    ItemState.COMPLETED,
    ItemState.ORDER_COMPLETE_NOT_NEEDED,
}
ACTIVE_PROVIDER_STATES = {
    ItemState.SUBMITTING,
    ItemState.PROVIDER_ACTIVE,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CandidatePoolError(RuntimeError):
    """Base error for durable pool violations."""


class CandidateIdentityConflict(CandidatePoolError):
    """The same candidate key was presented with different immutable identity."""


class SubmissionUnknown(CandidatePoolError):
    """A Provider create call did not return a trustworthy task ID."""


class RegistryConflict(CandidatePoolError):
    """A candidate conflicts with the permanent Registry/order identity."""


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class CandidateRecord:
    candidate_id: str
    order_id: str
    job_id: str
    record_id: str
    source: str
    source_product_id: str
    canonical_url: str
    preview_id: str
    preview_url: str
    capture_sha256: str
    image_sha256: str
    historical_status: str = "UNSEEN"
    media_status: str = "PENDING"
    visual_status: str = "PENDING"
    category_status: str = "PENDING"
    date_status: str = "PENDING"
    dimension_status: str = "PENDING"
    catalog_status: str = "PENDING"
    provider_status: str = "PENDING"
    failure_disposition: FailureDisposition = FailureDisposition.NONE
    rejection_reason: str | None = None
    retry_count: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    lineage: dict[str, Any] = field(default_factory=dict)
    state: ItemState = ItemState.DISCOVERED
    category_group: str | None = None
    product_name: str | None = None
    provider_task_id: str | None = None
    raw_glb_path: str | None = None
    raw_glb_sha256: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateRecord":
        values = dict(payload)
        values["failure_disposition"] = FailureDisposition(
            values.get("failure_disposition", FailureDisposition.NONE)
        )
        values["state"] = ItemState(values.get("state", ItemState.DISCOVERED))
        values["lineage"] = dict(values.get("lineage") or {})
        return cls(**{key: values[key] for key in cls.__dataclass_fields__ if key in values})

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_disposition"] = self.failure_disposition.value
        payload["state"] = self.state.value
        return payload

    @property
    def identity_key(self) -> str:
        return "|".join((self.source.casefold(), self.source_product_id.casefold(), self.canonical_url.casefold()))

    @property
    def model_input_hash(self) -> str | None:
        value = self.lineage.get("model_input_hash")
        return str(value) if value else None


class _ProcessLock:
    """Small cross-process lock for one workspace pool file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
        except (ImportError, OSError):
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                # Atomic replacement still protects readers; on platforms
                # without an advisory lock the caller must serialize workers.
                pass
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is None:
            return
        try:
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        self.handle.close()


class CandidatePoolStore:
    """Read/write the one durable candidate pool for an order."""

    _thread_lock = threading.RLock()

    def __init__(self, path: Path, *, order_id: str, job_id: str | None = None) -> None:
        self.path = Path(path).resolve()
        self.order_id = order_id
        self.job_id = job_id or order_id
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.events_path = self.path.with_name("candidate_pool_events.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _default(self) -> dict[str, Any]:
        return {
            "schema_version": POOL_SCHEMA_VERSION,
            "order_id": self.order_id,
            "job_id": self.job_id,
            "target_count": 0,
            "progressive_gates": [1, 3, 10, 20],
            "order_policy_hash": None,
            "items": {},
            "metrics": {},
            "job_status": "PENDING",
            "updated_at": utc_now(),
        }

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._default()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CandidatePoolError(f"candidate pool is unreadable: {self.path}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") not in {POOL_SCHEMA_VERSION, "candidate-pool.v1"}:
            raise CandidatePoolError("unsupported candidate pool schema")
        if str(payload.get("order_id") or "") != self.order_id:
            raise CandidatePoolError("candidate pool order_id does not match the current order")
        if payload.get("schema_version") == "candidate-pool.v1":
            payload = self._migrate_v1(payload)
        payload.setdefault("items", {})
        if not isinstance(payload["items"], dict):
            raise CandidatePoolError("candidate pool items must be an object")
        return payload

    @staticmethod
    def _migrate_v1(payload: dict[str, Any]) -> dict[str, Any]:
        migrated = dict(payload)
        items: dict[str, Any] = {}
        raw_items = payload.get("items") or []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("record_id") or "")
            if not record_id:
                continue
            item = dict(item)
            item.setdefault("candidate_id", record_id)
            item.setdefault("order_id", str(payload.get("project_id") or payload.get("order_id") or ""))
            item.setdefault("job_id", item["order_id"])
            item.setdefault("source", "unknown")
            item.setdefault("source_product_id", record_id)
            item.setdefault("canonical_url", item.get("url") or record_id)
            item.setdefault("preview_id", record_id)
            item.setdefault("preview_url", item.get("image_url") or "")
            item.setdefault("capture_sha256", "legacy")
            item.setdefault("image_sha256", str(item.get("source_asset_sha256") or "legacy"))
            item.setdefault("state", ItemState.DISCOVERED.value)
            item.setdefault("failure_disposition", FailureDisposition.NONE.value)
            item.setdefault("lineage", {"migrated_from": "candidate-pool.v1"})
            items[record_id] = item
        migrated["schema_version"] = POOL_SCHEMA_VERSION
        migrated["items"] = items
        migrated["job_id"] = migrated.get("job_id") or migrated.get("order_id")
        return migrated

    def read(self) -> dict[str, Any]:
        with self._thread_lock, _ProcessLock(self.lock_path):
            return self._read_unlocked()

    def records(self) -> list[CandidateRecord]:
        payload = self.read()
        return [CandidateRecord.from_dict(value) for value in payload["items"].values()]

    @contextmanager
    def _mutating(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock, _ProcessLock(self.lock_path):
            payload = self._read_unlocked()
            yield payload
            payload["updated_at"] = utc_now()
            self._write_unlocked(payload)

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp", delete=False
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        last_error: PermissionError | None = None
        for attempt in range(8):
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError as error:
                last_error = error
                if attempt == 7:
                    temporary.unlink(missing_ok=True)
                    raise
                # Windows readers (for example a live monitor) can briefly
                # hold the destination without making the durable state
                # invalid. Retry the atomic replace; never fall back to a
                # non-atomic overwrite.
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:  # pragma: no cover - defensive
            raise last_error

    def _event(self, event_type: str, *, candidate_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        record = {
            "schema_version": POOL_EVENT_SCHEMA_VERSION,
            "at": utc_now(),
            "order_id": self.order_id,
            "job_id": self.job_id,
            "event_type": event_type,
            "candidate_id": candidate_id,
            "payload": payload or {},
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def set_order_policy_hash(self, order_policy_hash: str, *, target_count: int, progressive_gates: Iterable[int]) -> None:
        if not order_policy_hash:
            raise ValueError("order policy hash is required")
        with self._mutating() as payload:
            existing = payload.get("order_policy_hash")
            if existing and existing != order_policy_hash:
                raise CandidateIdentityConflict("order policy hash cannot change after pool creation")
            payload["order_policy_hash"] = order_policy_hash
            payload["target_count"] = int(target_count)
            payload["progressive_gates"] = [int(gate) for gate in progressive_gates]
            self._event("order_policy_locked", payload={"order_policy_hash": order_policy_hash, "target_count": int(target_count)})

    def migrate_order_policy_hash(
        self,
        previous_order_policy_hash: str,
        order_policy_hash: str,
        *,
        target_count: int,
        progressive_gates: Iterable[int],
        reason: str,
    ) -> None:
        """Perform one explicit, no-provider state migration for a resumed job.

        Furniture Workflow 8.8 removes the historical category quotas without
        changing candidate identity or the Exact-N boundary.  A resumed 8.7
        pool therefore needs an auditable hash migration rather than being
        mistaken for a second order.  This method is intentionally narrow:
        callers must name the exact previous hash, no provider task may be
        active, and the event ledger records both hashes and the reason.
        """

        previous = str(previous_order_policy_hash or "").strip()
        current = str(order_policy_hash or "").strip()
        if not previous or not current or previous == current:
            raise ValueError("a distinct previous and current order policy hash are required")
        if self.active_provider_count():
            raise CandidateIdentityConflict("order policy cannot migrate while Provider tasks are active")
        with self._mutating() as payload:
            existing = str(payload.get("order_policy_hash") or "").strip()
            if existing != previous:
                raise CandidateIdentityConflict("previous order policy hash does not match the candidate pool")
            payload["order_policy_hash"] = current
            payload["target_count"] = int(target_count)
            payload["progressive_gates"] = [int(gate) for gate in progressive_gates]
            self._event(
                "order_policy_migrated",
                payload={
                    "previous_order_policy_hash": previous,
                    "order_policy_hash": current,
                    "target_count": int(target_count),
                    "reason": str(reason or ""),
                },
            )

    def add_candidates(self, values: Iterable[CandidateRecord | dict[str, Any]]) -> dict[str, int]:
        added = duplicates = conflicts = 0
        with self._mutating() as payload:
            items = payload["items"]
            identities = {
                str(value.get("source") or "").casefold() + "|" + str(value.get("source_product_id") or "").casefold() + "|" + str(value.get("canonical_url") or "").casefold()
                for value in items.values()
                if isinstance(value, dict)
            }
            for incoming in values:
                record = incoming if isinstance(incoming, CandidateRecord) else CandidateRecord.from_dict(incoming)
                if record.order_id != self.order_id or not record.record_id or not record.canonical_url:
                    raise ValueError("candidate identity is incomplete or belongs to another order")
                existing = items.get(record.candidate_id)
                if existing is not None:
                    old = CandidateRecord.from_dict(existing)
                    if old.identity_key != record.identity_key or old.image_sha256 != record.image_sha256:
                        conflicts += 1
                        raise CandidateIdentityConflict(f"candidate_id identity changed: {record.candidate_id}")
                    duplicates += 1
                    self._event("candidate_duplicate", candidate_id=record.candidate_id)
                    continue
                if record.identity_key in identities:
                    duplicates += 1
                    self._event("candidate_duplicate", candidate_id=record.candidate_id, payload={"identity_key": record.identity_key})
                    continue
                items[record.candidate_id] = record.to_dict()
                identities.add(record.identity_key)
                added += 1
                self._event("candidate_discovered", candidate_id=record.candidate_id, payload={"record_id": record.record_id})
        return {"added": added, "duplicates": duplicates, "conflicts": conflicts}

    def _update_record(self, payload: dict[str, Any], candidate_id: str) -> CandidateRecord:
        raw = payload["items"].get(candidate_id)
        if not isinstance(raw, dict):
            raise KeyError(candidate_id)
        return CandidateRecord.from_dict(raw)

    def transition(
        self,
        candidate_id: str,
        state: ItemState,
        *,
        stage_field: str | None = None,
        stage_status: str | None = None,
        reason: str | None = None,
        disposition: FailureDisposition = FailureDisposition.NONE,
        retry: bool = False,
        lineage: dict[str, Any] | None = None,
    ) -> CandidateRecord:
        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if record.state is ItemState.COMPLETED and state is not ItemState.COMPLETED:
                raise CandidatePoolError("completed candidate cannot move backwards")
            record.state = ItemState(state)
            if stage_field:
                if stage_field not in {
                    "historical_status", "media_status", "visual_status", "category_status",
                    "date_status", "dimension_status", "catalog_status", "provider_status",
                }:
                    raise ValueError(f"unsupported candidate stage field: {stage_field}")
                setattr(record, stage_field, stage_status or state.value)
            record.failure_disposition = FailureDisposition(disposition)
            record.rejection_reason = reason or record.rejection_reason
            record.retry_count += 1 if retry else 0
            if lineage:
                record.lineage.update(lineage)
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "candidate_transition",
                candidate_id=candidate_id,
                payload={"state": record.state.value, "reason": reason, "disposition": record.failure_disposition.value},
            )
            return record

    def enrich_candidate(
        self,
        candidate_id: str,
        *,
        category_group: str | None = None,
        product_name: str | None = None,
        lineage: dict[str, Any] | None = None,
    ) -> CandidateRecord:
        """Persist governed qualification facts without inventing a state transition."""

        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if category_group is not None:
                record.category_group = str(category_group).strip() or record.category_group
            if product_name is not None:
                record.product_name = str(product_name).strip() or record.product_name
            if lineage:
                record.lineage.update(lineage)
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "candidate_enriched",
                candidate_id=candidate_id,
                payload={
                    "category_group": record.category_group,
                    "product_name": record.product_name,
                    "lineage_keys": sorted((lineage or {}).keys()),
                },
            )
            return record

    def mark_provider_task(self, candidate_id: str, provider_task_id: str | None, *, provider: str) -> CandidateRecord:
        if not str(provider_task_id or "").strip():
            self.quarantine_submission_unknown(
                candidate_id,
                reason="Provider create did not return provider_task_id",
            )
            raise SubmissionUnknown(f"provider task ID missing for {candidate_id}")
        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            record.provider_task_id = str(provider_task_id)
            record.provider_status = "ACTIVE"
            record.state = ItemState.PROVIDER_ACTIVE
            record.failure_disposition = FailureDisposition.WAIT_PROVIDER
            record.updated_at = utc_now()
            record.lineage["provider"] = provider
            payload["items"][candidate_id] = record.to_dict()
            self._event("provider_submitted", candidate_id=candidate_id, payload={"provider_task_id": str(provider_task_id), "provider": provider})
            return record

    def quarantine_submission_unknown(self, candidate_id: str, *, reason: str) -> CandidateRecord:
        """Reserve one Exact-N slot without treating an ambiguous POST as failure."""

        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if record.state is ItemState.COMPLETED:
                raise CandidatePoolError("completed candidate cannot be quarantined")
            if record.provider_task_id:
                raise CandidatePoolError("submission_unknown quarantine cannot contain a provider task ID")
            if record.state is ItemState.QUARANTINED_SUBMISSION_UNKNOWN:
                return record
            record.state = ItemState.QUARANTINED_SUBMISSION_UNKNOWN
            record.provider_status = "SUBMISSION_UNKNOWN"
            record.failure_disposition = FailureDisposition.WAIT_PROVIDER
            record.rejection_reason = reason or record.rejection_reason or "submission_unknown"
            record.lineage.setdefault("submission_unknown_quarantine", {
                "reason": record.rejection_reason,
                "quarantined_at": utc_now(),
                "provider_task_id": None,
            })
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "submission_unknown_quarantined",
                candidate_id=candidate_id,
                payload={"reason": record.rejection_reason, "provider_task_id": None},
            )
            return record

    def abandon_submission_unknown(self, candidate_id: str, *, reason: str) -> CandidateRecord:
        """Permanently retire one reconciled submission_unknown without resubmission.

        This transition is intentionally one-way.  It releases the Exact-N
        reservation only after an external reconciliation decision, preserves
        the original quarantine evidence, and leaves the record terminal so it
        can never be selected as replacement supply.
        """

        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if record.state is ItemState.ABANDONED_SUBMISSION_UNKNOWN:
                return record
            if record.state is not ItemState.QUARANTINED_SUBMISSION_UNKNOWN:
                raise CandidatePoolError(
                    "only a quarantined submission_unknown may be abandoned"
                )
            if record.provider_task_id:
                raise CandidatePoolError(
                    "submission_unknown with a Provider task ID must be queried, not abandoned"
                )
            original = {
                "state": record.state.value,
                "provider_status": record.provider_status,
                "failure_disposition": record.failure_disposition.value,
                "rejection_reason": record.rejection_reason,
                "quarantine": dict(
                    record.lineage.get("submission_unknown_quarantine") or {}
                ),
            }
            record.state = ItemState.ABANDONED_SUBMISSION_UNKNOWN
            record.provider_status = "ABANDONED_SUBMISSION_UNKNOWN"
            record.failure_disposition = FailureDisposition.NONE
            record.rejection_reason = reason or "submission_unknown abandoned after final reconciliation"
            record.lineage["submission_unknown_abandonment"] = {
                "reason": record.rejection_reason,
                "abandoned_at": utc_now(),
                "provider_task_id": None,
                "exact_n_reservation_released": True,
                "never_resubmit": True,
                "original_evidence": original,
            }
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "submission_unknown_abandoned",
                candidate_id=candidate_id,
                payload={
                    "reason": record.rejection_reason,
                    "provider_task_id": None,
                    "exact_n_reservation_released": True,
                    "never_resubmit": True,
                },
            )
            return record

    def mark_raw_glb(
        self,
        candidate_id: str,
        *,
        raw_glb_path: str,
        raw_glb_sha256: str,
        valid: bool,
        model_input_hash: str | None = None,
    ) -> CandidateRecord:
        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if not valid:
                record.state = ItemState.RAW_GLB_INVALID
                record.provider_status = "FAILED"
                record.failure_disposition = FailureDisposition.REPLACE_CANDIDATE
                record.rejection_reason = "raw_glb_invalid"
                self._event("raw_glb_invalid", candidate_id=candidate_id)
            else:
                known_hashes = {
                    str(value.get("raw_glb_sha256"))
                    for value in payload["items"].values()
                    if isinstance(value, dict) and value.get("raw_glb_sha256")
                }
                if raw_glb_sha256 in known_hashes:
                    record.state = ItemState.RAW_GLB_INVALID
                    record.provider_status = "FAILED"
                    record.failure_disposition = FailureDisposition.REPLACE_CANDIDATE
                    record.rejection_reason = "duplicate_output_hash"
                    self._event("raw_glb_duplicate", candidate_id=candidate_id, payload={"sha256": raw_glb_sha256})
                else:
                    record.raw_glb_path = raw_glb_path
                    record.raw_glb_sha256 = raw_glb_sha256
                    record.provider_status = "SUCCESS"
                    record.state = ItemState.COMPLETED
                    record.failure_disposition = FailureDisposition.NONE
                    if model_input_hash:
                        record.lineage["model_input_hash"] = model_input_hash
                    self._event("raw_glb_ready", candidate_id=candidate_id, payload={"sha256": raw_glb_sha256, "path": raw_glb_path})
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            return record

    def success_count(self) -> int:
        return sum(1 for item in self.records() if item.state is ItemState.COMPLETED and item.raw_glb_sha256)

    def active_provider_count(self) -> int:
        return sum(1 for item in self.records() if item.state in ACTIVE_PROVIDER_STATES and item.provider_task_id)

    def unresolved_submission_unknown_count(self) -> int:
        return sum(
            1
            for item in self.records()
            if is_submission_unknown_marker(
                state=item.state,
                provider_status=item.provider_status,
                reason=item.rejection_reason,
            )
        )

    def retire_quota_locked(self, candidate_id: str, *, category: str, reason: str = "QUOTA_ALREADY_FILLED") -> CandidateRecord:
        """Retire an unsubmitted model lock after its category quota is full."""
        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if record.state is ItemState.COMPLETED or record.state is not ItemState.MODEL_INPUT_LOCKED:
                return record
            record.state = ItemState.CATEGORY_REJECTED
            record.category_status = "QUOTA_ALREADY_FILLED"
            record.provider_status = "NOT_SUBMITTED"
            record.failure_disposition = FailureDisposition.REPLACE_CANDIDATE
            record.rejection_reason = reason
            record.lineage["quota_retirement"] = {"category": category, "reason": reason}
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "candidate_quota_retired", candidate_id=candidate_id,
                payload={"category": category, "reason": reason, "provider_posted": False},
            )
            return record

    def retire_order_complete_not_needed(
        self, candidate_id: str, *, reason: str = "ORDER_COMPLETE_NOT_NEEDED"
    ) -> CandidateRecord:
        """Close an unsubmitted model lock after the Exact-N order is complete.

        This is a terminal bookkeeping transition only.  It never creates a
        Provider task and refuses to retire an item that already has one.
        """
        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if record.state in {ItemState.COMPLETED, ItemState.ORDER_COMPLETE_NOT_NEEDED}:
                return record
            if record.provider_task_id or record.provider_status not in {"PENDING", "NOT_SUBMITTED"}:
                raise CandidatePoolError("cannot retire a candidate with Provider activity")
            if record.state is not ItemState.MODEL_INPUT_LOCKED:
                raise CandidatePoolError("only an unsubmitted model input may be retired")
            record.state = ItemState.ORDER_COMPLETE_NOT_NEEDED
            record.provider_status = "NOT_SUBMITTED"
            record.failure_disposition = FailureDisposition.NONE
            record.rejection_reason = reason
            record.lineage["order_completion_retirement"] = {
                "reason": reason,
                "provider_posted": False,
            }
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "candidate_order_complete_not_needed",
                candidate_id=candidate_id,
                payload={"reason": reason, "provider_posted": False},
            )
            return record

    def retire_out_of_scope(
        self, candidate_id: str, *, reason: str = "FROZEN_CATALOG_SCOPE_EXCLUDED"
    ) -> CandidateRecord:
        """Retire an unsubmitted candidate outside the current frozen scope.

        A resumed job can contain an older discovery backlog in its durable
        pool.  Once a FrozenCatalogSnapshot is authoritative, those records
        must remain auditable but must not be offered to the generation
        engine.  This transition never accepts a record with Provider
        activity and never changes a completed record.
        """
        with self._mutating() as payload:
            record = self._update_record(payload, candidate_id)
            if record.state in TERMINAL_ITEM_STATES:
                return record
            if record.provider_task_id or record.state in ACTIVE_PROVIDER_STATES:
                raise CandidatePoolError("cannot retire an out-of-scope candidate with Provider activity")
            record.state = ItemState.ORDER_COMPLETE_NOT_NEEDED
            record.provider_status = "NOT_SUBMITTED"
            record.failure_disposition = FailureDisposition.NONE
            record.rejection_reason = reason
            record.lineage["scope_retirement"] = {
                "reason": reason,
                "provider_posted": False,
            }
            record.updated_at = utc_now()
            payload["items"][candidate_id] = record.to_dict()
            self._event(
                "candidate_scope_retired",
                candidate_id=candidate_id,
                payload={"reason": reason, "provider_posted": False},
            )
            return record

    def refill_needed(self, target_count: int) -> int:
        return max(
            0,
            int(target_count)
            - self.success_count()
            - self.active_provider_count()
            - self.unresolved_submission_unknown_count(),
        )

    def available(self) -> list[CandidateRecord]:
        values = [item for item in self.records() if item.state not in TERMINAL_ITEM_STATES and item.state not in ACTIVE_PROVIDER_STATES]
        return sorted(values, key=lambda item: (item.created_at, item.candidate_id))

    def set_job_status(self, status: str, *, reason: str | None = None) -> None:
        with self._mutating() as payload:
            payload["job_status"] = status
            self._event("job_status", payload={"status": status, "reason": reason})

    def record_refill(self, *, requested: int, added: int, reason: str) -> None:
        """Persist bounded replacement-supply accounting without changing history."""
        with self._mutating() as payload:
            metrics = payload.setdefault("metrics", {})
            metrics["refill_rounds"] = int(metrics.get("refill_rounds") or 0) + 1
            metrics["refill_requested"] = int(metrics.get("refill_requested") or 0) + int(requested)
            metrics["refill_added"] = int(metrics.get("refill_added") or 0) + int(added)
            self._event(
                "candidate_refill",
                payload={"requested": int(requested), "added": int(added), "reason": reason},
            )

    def record_gate(self, gate: int, *, chain: list[str], code_revision: str | None = None) -> None:
        with self._mutating() as payload:
            receipts = payload.setdefault("progressive_gate_receipts", [])
            if any(
                int(item.get("gate") or 0) == int(gate) and item.get("status") == "PASS"
                for item in receipts if isinstance(item, dict)
            ):
                return
            receipt = {
                "gate": int(gate),
                "status": "PASS",
                "completed_raw_glb": sum(
                    1 for item in payload["items"].values()
                    if isinstance(item, dict)
                    and item.get("state") == ItemState.COMPLETED.value
                    and item.get("raw_glb_sha256")
                ),
                "verified_chain": list(chain),
                "code_revision": code_revision,
                "recorded_at": utc_now(),
            }
            receipts.append(receipt)
            self._event("progressive_gate_pass", payload=receipt)

    def gate_receipts(self) -> list[dict[str, Any]]:
        payload = self.read()
        values = payload.get("progressive_gate_receipts") or []
        return [dict(item) for item in values if isinstance(item, dict)]

    def summary(self) -> dict[str, Any]:
        records = self.records()
        state_counts: dict[str, int] = {}
        disposition_counts: dict[str, int] = {}
        for record in records:
            state_counts[record.state.value] = state_counts.get(record.state.value, 0) + 1
            disposition_counts[record.failure_disposition.value] = disposition_counts.get(record.failure_disposition.value, 0) + 1
        payload = self.read()
        return {
            "schema_version": POOL_SCHEMA_VERSION,
            "order_id": self.order_id,
            "job_id": self.job_id,
            "target_count": int(payload.get("target_count") or 0),
            "job_status": str(payload.get("job_status") or "PENDING"),
            "total_candidates": len(records),
            "success_count": self.success_count(),
            "active_provider_tasks": self.active_provider_count(),
            "available_count": len(self.available()),
            "unresolved_submission_unknown": self.unresolved_submission_unknown_count(),
            "state_counts": state_counts,
            "failure_disposition_counts": disposition_counts,
            "metrics": dict(payload.get("metrics") or {}),
            "order_policy_hash": payload.get("order_policy_hash"),
            "updated_at": payload.get("updated_at"),
        }


__all__ = [
    "ACTIVE_PROVIDER_STATES",
    "CandidateIdentityConflict",
    "CandidatePoolError",
    "CandidatePoolStore",
    "CandidateRecord",
    "POOL_EVENT_SCHEMA_VERSION",
    "POOL_SCHEMA_VERSION",
    "RegistryConflict",
    "SubmissionUnknown",
    "TERMINAL_ITEM_STATES",
]
