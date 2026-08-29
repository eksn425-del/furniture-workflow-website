"""Immutable order and model-input locks shared by both entry interfaces."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


LOCK_SCHEMA_VERSION = "workflow-locks.v1"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stable_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class OrderPolicyLock:
    source: str
    categories: dict[str, int]
    exact_n: int
    provider: str
    ruleset: str
    image_policy: str
    five_year_policy: str
    naming_policy: str
    dimension_policy: str
    registry_identity: str
    registry_version: str
    authorization_mode: str
    quality_policy: str
    created_at: str
    category_quota_mode: str = "REQUIRED"
    policy_revision: str = "8.8.1"
    allowed_product_scope: str = "LEGACY"
    schema_version: str = LOCK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "categories": dict(sorted(self.categories.items())),
            "exact_n": self.exact_n,
            "provider": self.provider,
            "ruleset": self.ruleset,
            "image_policy": self.image_policy,
            "five_year_policy": self.five_year_policy,
            "naming_policy": self.naming_policy,
            "dimension_policy": self.dimension_policy,
            "registry_identity": self.registry_identity,
            "registry_version": self.registry_version,
            "authorization_mode": self.authorization_mode,
            "quality_policy": self.quality_policy,
            "category_quota_mode": self.category_quota_mode,
            "policy_revision": self.policy_revision,
            "allowed_product_scope": self.allowed_product_scope,
            "created_at": self.created_at,
        }

    @property
    def order_policy_hash(self) -> str:
        # Creation time is audit metadata, not policy identity.  Excluding it
        # makes a restarted interface derive the same lock for the same order.
        payload = self.to_dict()
        payload.pop("created_at", None)
        return stable_hash(payload)


def make_order_policy_lock(
    *, source: str, categories: dict[str, int], exact_n: int, provider: str,
    ruleset: str, image_policy: str, five_year_policy: str,
    naming_policy: str, dimension_policy: str, registry_identity: str,
    registry_version: str, authorization_mode: str, quality_policy: str,
    category_quota_mode: str = "REQUIRED",
    policy_revision: str = "8.8.1",
    allowed_product_scope: str = "LEGACY",
) -> OrderPolicyLock:
    quota_mode = str(category_quota_mode or "REQUIRED").upper()
    if exact_n <= 0:
        raise ValueError("Exact-N must be positive")
    if quota_mode != "NONE" and sum(categories.values()) != exact_n:
        raise ValueError("category quota sum must equal Exact-N")
    if quota_mode == "NONE" and categories:
        raise ValueError("category quotas must be empty when category_quota_mode is NONE")
    return OrderPolicyLock(
        source=source,
        categories=dict(categories),
        exact_n=int(exact_n),
        provider=provider,
        ruleset=ruleset,
        image_policy=image_policy,
        five_year_policy=five_year_policy,
        naming_policy=naming_policy,
        dimension_policy=dimension_policy,
        registry_identity=registry_identity,
        registry_version=registry_version,
        authorization_mode=authorization_mode,
        quality_policy=quality_policy,
        created_at=_now(),
        category_quota_mode=quota_mode,
        policy_revision=str(policy_revision),
        allowed_product_scope=str(allowed_product_scope),
    )


@dataclass(frozen=True)
class ModelInputLock:
    order_id: str
    candidate_id: str
    record_id: str
    canonical_url: str
    image_sha256: str
    visual_receipt_sha256: str
    category_evidence_sha256: str
    style: str
    color: str
    material: str
    product_type: str
    product_name: str
    width: float
    depth: float
    height: float
    dimension_evidence_sha256: str
    catalog_evidence_sha256: str
    provider: str
    model_input_hash: str
    schema_version: str = LOCK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["schema_version"] = self.schema_version
        return payload


def make_model_input_lock(**values: Any) -> ModelInputLock:
    required = {
        "order_id", "candidate_id", "record_id", "canonical_url", "image_sha256",
        "visual_receipt_sha256", "category_evidence_sha256", "style", "color", "material",
        "product_type", "product_name", "width", "depth", "height", "dimension_evidence_sha256",
        "catalog_evidence_sha256", "provider",
    }
    missing = sorted(key for key in required if not values.get(key) and key not in {"width", "depth", "height"})
    if missing:
        raise ValueError(f"model input lock is missing: {', '.join(missing)}")
    preliminary = dict(values)
    preliminary.pop("model_input_hash", None)
    preliminary.pop("schema_version", None)
    values["model_input_hash"] = stable_hash(preliminary)
    return ModelInputLock(**values)


def provider_idempotency_key(*, order_id: str, record_id: str, model_input_hash: str, provider: str) -> str:
    return stable_hash({
        "order_id": order_id,
        "record_id": record_id,
        "model_input_hash": model_input_hash,
        "provider": provider,
    })


__all__ = [
    "LOCK_SCHEMA_VERSION",
    "ModelInputLock",
    "OrderPolicyLock",
    "make_model_input_lock",
    "make_order_policy_lock",
    "provider_idempotency_key",
    "stable_hash",
]
