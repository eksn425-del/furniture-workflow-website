"""Deterministic identity for one paid model-generation scope."""

from __future__ import annotations

from typing import Any

from .locks import stable_hash


GENERATION_SCOPE_SCHEMA_VERSION = "generation-scope.v1"


def generation_scope_payload(
    *, project_id: str, catalog_content_hash: str, provider: str, exact_n_target: int
) -> dict[str, Any]:
    return {
        "schema_version": GENERATION_SCOPE_SCHEMA_VERSION,
        "project_id": str(project_id),
        "catalog_content_hash": str(catalog_content_hash),
        "provider": str(provider).strip().lower(),
        "exact_n_target": int(exact_n_target),
    }


def generation_scope_key(
    *, project_id: str, catalog_content_hash: str, provider: str, exact_n_target: int
) -> str:
    return stable_hash(generation_scope_payload(
        project_id=project_id,
        catalog_content_hash=catalog_content_hash,
        provider=provider,
        exact_n_target=exact_n_target,
    ))


__all__ = [
    "GENERATION_SCOPE_SCHEMA_VERSION",
    "generation_scope_key",
    "generation_scope_payload",
]
