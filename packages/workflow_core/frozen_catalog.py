"""Immutable catalog snapshot shared by the Website production stages.

The catalog lock written during Phase A contains useful audit metadata, but it
also contains timestamps and is historically written by more than one worker.
This module provides the one immutable, business-content identity that paid
generation is allowed to consume after Phase A has completed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .locks import stable_hash


FROZEN_CATALOG_SCHEMA_VERSION = "frozen-catalog.v1"
FROZEN_CATALOG_RELATIVE_PATH = Path("03_models") / "state" / "frozen_catalog_snapshot.json"


class FrozenCatalogConflict(RuntimeError):
    """Raised when an existing immutable snapshot would be changed."""


class FrozenCatalogValidationError(ValueError):
    """Raised when the Phase-A artifacts cannot form a safe snapshot."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def frozen_catalog_path(workspace: Path) -> Path:
    return Path(workspace) / FROZEN_CATALOG_RELATIVE_PATH


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_float(value: Any, *, field: str, record_id: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FrozenCatalogValidationError(
            f"record {record_id} has invalid {field} dimension"
        ) from exc
    if number <= 0:
        raise FrozenCatalogValidationError(f"record {record_id} has non-positive {field} dimension")
    return number


def _record_value(catalog: dict[str, Any], model: dict[str, Any], key: str, fallback: str = "") -> str:
    value = model.get(key)
    if value in (None, ""):
        value = catalog.get(key, fallback)
    return str(value or "")


def _merge_record(catalog: dict[str, Any], model: dict[str, Any], order_id: str) -> dict[str, Any]:
    record_id = str(catalog.get("record_id") or model.get("record_id") or "").strip()
    if not record_id or str(model.get("record_id") or record_id) != record_id:
        raise FrozenCatalogValidationError("catalog and model-input record identity mismatch")

    canonical_url = _record_value(catalog, model, "canonical_url")
    product_url = _record_value(catalog, model, "product_url", canonical_url)
    if not canonical_url:
        raise FrozenCatalogValidationError(f"record {record_id} is missing canonical_url")

    product_name = _record_value(model, catalog, "product_name", _record_value(catalog, model, "name"))
    product_type = _record_value(model, catalog, "product_type")
    style = _record_value(model, catalog, "style")
    color = _record_value(model, catalog, "color")
    material = _record_value(model, catalog, "material")
    image_sha256 = _record_value(model, catalog, "image_sha256", _record_value(catalog, model, "source_asset_sha256"))
    model_input_hash = _record_value(model, catalog, "model_input_hash")
    filename_stem = _record_value(catalog, model, "filename_stem")
    missing = [
        name for name, value in {
            "product_name": product_name,
            "product_type": product_type,
            "style": style,
            "color": color,
            "material": material,
            "image_sha256": image_sha256,
            "model_input_hash": model_input_hash,
            "filename_stem": filename_stem,
        }.items() if not value
    ]
    if missing:
        raise FrozenCatalogValidationError(f"record {record_id} is missing: {', '.join(missing)}")

    return {
        "order_id": order_id,
        "record_id": record_id,
        "identity_key": _record_value(catalog, model, "identity_key"),
        "canonical_url": canonical_url,
        "product_url": product_url,
        "image_url": _record_value(catalog, model, "image_url"),
        "image_sha256": image_sha256,
        "source_asset_sha256": _record_value(catalog, model, "source_asset_sha256", image_sha256),
        "filename_stem": filename_stem,
        "product_name": product_name,
        "name": _record_value(catalog, model, "name", product_name),
        "product_type": product_type,
        "style": style,
        "color": color,
        "material": material,
        "width": _as_float(model.get("width"), field="width", record_id=record_id),
        "depth": _as_float(model.get("depth"), field="depth", record_id=record_id),
        "height": _as_float(model.get("height"), field="height", record_id=record_id),
        "size": _record_value(catalog, model, "size"),
        "visual_receipt_sha256": _record_value(model, catalog, "visual_receipt_sha256"),
        "category_evidence_sha256": _record_value(model, catalog, "category_evidence_sha256"),
        "dimension_evidence_sha256": _record_value(model, catalog, "dimension_evidence_sha256"),
        "catalog_evidence_sha256": _record_value(model, catalog, "catalog_evidence_sha256"),
        "provider": _record_value(model, catalog, "provider", "lux3d"),
        "model_input_hash": model_input_hash,
    }


def canonical_frozen_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only business fields in deterministic identity order."""

    volatile_free = []
    for record in records:
        volatile_free.append({key: record.get(key) for key in sorted(record) if key not in {"order_id"}})
    return sorted(volatile_free, key=lambda item: str(item.get("record_id", "")))


def catalog_content_hash(records: list[dict[str, Any]]) -> str:
    payload = {
        "schema_version": FROZEN_CATALOG_SCHEMA_VERSION,
        "records": canonical_frozen_records(records),
    }
    return stable_hash(payload)


def read_frozen_catalog(workspace: Path) -> dict[str, Any] | None:
    path = frozen_catalog_path(workspace)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenCatalogValidationError(f"cannot read frozen catalog: {path}") from exc
    if payload.get("schema_version") != FROZEN_CATALOG_SCHEMA_VERSION or payload.get("status") != "frozen":
        raise FrozenCatalogValidationError("frozen catalog schema/status is invalid")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise FrozenCatalogValidationError("frozen catalog has no records")
    actual = catalog_content_hash(records)
    if actual != payload.get("catalog_content_hash"):
        raise FrozenCatalogValidationError("frozen catalog content hash does not match records")
    return payload


def frozen_catalog_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    records = list(snapshot.get("records") or [])
    return {
        "status": "frozen",
        "frozen": True,
        "ready_rows": len(records),
        "blocked_rows": 0,
        "rejected_rows": 0,
        "records": records,
        "content_sha256": snapshot["catalog_content_hash"],
        "catalog_content_hash": snapshot["catalog_content_hash"],
        "frozen_catalog_path": str(FROZEN_CATALOG_RELATIVE_PATH).replace("\\", "/"),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def freeze_catalog_snapshot(
    workspace: Path,
    *,
    order_id: str,
    exact_n_target: int,
) -> dict[str, Any]:
    """Create the immutable snapshot once, or return the identical snapshot."""

    existing = read_frozen_catalog(workspace)
    if existing is not None:
        if str(existing.get("order_id")) != str(order_id):
            raise FrozenCatalogConflict("frozen catalog belongs to a different order")
        return existing

    evidence_path = Path(workspace) / "02_repair" / "evidence" / "catalog_lock.json"
    model_inputs_path = Path(workspace) / "02_repair" / "model_input_locks.json"
    catalog_path = Path(workspace) / "02_repair" / "catalog_standard.xlsx"
    if not evidence_path.exists() or not model_inputs_path.exists():
        raise FrozenCatalogValidationError("Phase-A catalog lock and model-input locks are required")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    model_inputs = json.loads(model_inputs_path.read_text(encoding="utf-8"))
    catalog_records = evidence.get("records") or []
    input_records = model_inputs.get("records") or []
    if not catalog_records or len(catalog_records) != len(input_records):
        raise FrozenCatalogValidationError("catalog and model-input record counts do not match")

    by_id: dict[str, dict[str, Any]] = {}
    for item in input_records:
        record_id = str(item.get("record_id") or "")
        if not record_id or record_id in by_id:
            raise FrozenCatalogValidationError("model-input record IDs must be present and unique")
        by_id[record_id] = item

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in catalog_records:
        record_id = str(item.get("record_id") or "")
        if not record_id or record_id in seen or record_id not in by_id:
            raise FrozenCatalogValidationError("catalog/model-input record identity mismatch")
        seen.add(record_id)
        records.append(_merge_record(item, by_id[record_id], str(order_id)))

    if len(seen) != len(by_id):
        raise FrozenCatalogValidationError("model-input contains records absent from catalog")
    stems = [record["filename_stem"] for record in records]
    if len(stems) != len(set(stems)):
        raise FrozenCatalogValidationError("frozen catalog filename_stem values must be unique")

    content_hash = catalog_content_hash(records)
    snapshot = {
        "schema_version": FROZEN_CATALOG_SCHEMA_VERSION,
        "status": "frozen",
        "order_id": str(order_id),
        "exact_n_target": int(exact_n_target),
        "record_count": len(records),
        "records": records,
        "catalog_content_hash": content_hash,
        "source_catalog_lock_file_sha256": sha256_file(evidence_path),
        "source_catalog_content_hash": ((evidence.get("catalog") or {}).get("content_sha256")),
        "frozen_at": _now(),
        "catalog": {
            "path": "02_repair/catalog_standard.xlsx",
            "rows": len(records),
            "columns": ((evidence.get("catalog") or {}).get("columns") or []),
            "file_sha256": sha256_file(catalog_path) if catalog_path.exists() else None,
        },
    }

    path = frozen_catalog_path(workspace)
    if path.exists():
        raced = read_frozen_catalog(workspace)
        if raced is None or raced.get("catalog_content_hash") != content_hash:
            raise FrozenCatalogConflict("another writer created a different frozen catalog")
        return raced
    _atomic_write_json(path, snapshot)
    return read_frozen_catalog(workspace) or snapshot


__all__ = [
    "FROZEN_CATALOG_RELATIVE_PATH",
    "FROZEN_CATALOG_SCHEMA_VERSION",
    "FrozenCatalogConflict",
    "FrozenCatalogValidationError",
    "catalog_content_hash",
    "canonical_frozen_records",
    "freeze_catalog_snapshot",
    "frozen_catalog_path",
    "frozen_catalog_result",
    "read_frozen_catalog",
    "sha256_file",
]
