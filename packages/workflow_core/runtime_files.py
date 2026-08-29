"""Filesystem bindings for the single 8.7 Workflow Engine."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .candidate_pool import CandidatePoolStore
from .locks import OrderPolicyLock


RUNTIME_SCHEMA_VERSION = "workflow-runtime.v1"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def initialize_runtime(
    *,
    state_root: Path,
    order_id: str,
    job_id: str,
    policy: OrderPolicyLock,
    progressive_gates: tuple[int, ...],
    allow_streaming_policy_migration: bool = False,
) -> CandidatePoolStore:
    """Create or resume the shared lock + pool without touching the Registry.

    The optional migration path is only for the explicit 8.7 -> 8.8
    streaming-policy transition of the same paused job.  It preserves the
    previous lock in a local audit file and refuses to run while Provider work
    is active.
    """
    state_root = Path(state_root)
    lock_path = state_root / "order_policy_lock.json"
    existing_lock: dict[str, Any] | None = None
    if lock_path.is_file():
        existing_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        if str(existing_lock.get("order_policy_hash") or "") != policy.order_policy_hash:
            can_migrate = (
                allow_streaming_policy_migration
                and policy.category_quota_mode == "NONE"
                and str(policy.policy_revision).startswith("8.8.")
                and str(existing_lock.get("source") or "") == policy.source
                and int(existing_lock.get("exact_n") or 0) == policy.exact_n
                and str(existing_lock.get("provider") or "") == policy.provider
                and str(existing_lock.get("registry_version") or "") == policy.registry_version
            )
            if not can_migrate:
                raise ValueError("order policy lock conflicts with the existing order")
    else:
        _atomic_json(lock_path, {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            **policy.to_dict(),
            "order_id": order_id,
            "job_id": job_id,
            "order_policy_hash": policy.order_policy_hash,
            "progressive_gates": list(progressive_gates),
        })
    pool = CandidatePoolStore(state_root / "candidate_pool.json", order_id=order_id, job_id=job_id)
    if existing_lock is not None and str(existing_lock.get("order_policy_hash") or "") != policy.order_policy_hash:
        previous_hash = str(existing_lock.get("order_policy_hash") or "")
        if not previous_hash:
            raise ValueError("existing order policy lock has no hash")
        audit_path = state_root / "audit" / "order_policy_lock_pre_8_8.json"
        if not audit_path.is_file():
            _atomic_json(audit_path, existing_lock)
        pool.migrate_order_policy_hash(
            previous_hash,
            policy.order_policy_hash,
            target_count=policy.exact_n,
            progressive_gates=progressive_gates,
            reason="8.7 paused Skills Job resumed under 8.8 streaming furniture policy",
        )
        _atomic_json(lock_path, {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            **policy.to_dict(),
            "order_id": order_id,
            "job_id": job_id,
            "order_policy_hash": policy.order_policy_hash,
            "progressive_gates": list(progressive_gates),
            "migrated_from_order_policy_hash": previous_hash,
            "migration_audit": str(audit_path),
        })
        return pool
    pool.set_order_policy_hash(
        policy.order_policy_hash,
        target_count=policy.exact_n,
        progressive_gates=progressive_gates,
    )
    return pool


__all__ = ["RUNTIME_SCHEMA_VERSION", "initialize_runtime"]
