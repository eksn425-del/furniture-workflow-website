from __future__ import annotations

import json
from pathlib import Path

from packages.workflow_core.frozen_catalog import (
    FROZEN_CATALOG_SCHEMA_VERSION,
    catalog_content_hash,
    freeze_catalog_snapshot,
    read_frozen_catalog,
)
from packages.workflow_core.generation_scope import generation_scope_key


def _record(record_id: str, *, name: str = "Modern Black Plastic Chair", stem: str | None = None) -> dict:
    return {
        "order_id": "project_test_frozen_catalog_001",
        "record_id": record_id,
        "identity_key": f"cgtrader|{record_id}",
        "canonical_url": f"https://www.cgtrader.com/products/{record_id}",
        "product_url": f"https://www.cgtrader.com/products/{record_id}",
        "image_url": f"https://img.example/{record_id}.jpg",
        "image_sha256": f"image-{record_id}",
        "source_asset_sha256": f"source-{record_id}",
        "filename_stem": stem or f"{name.lower().replace(' ', '-')}-{record_id}",
        "product_name": name,
        "name": name,
        "product_type": "Chair",
        "style": "Modern",
        "color": "Black",
        "material": "Plastic",
        "width": 18.0,
        "depth": 20.0,
        "height": 32.0,
        "size": "18 x 20 x 32",
        "visual_receipt_sha256": f"visual-{record_id}",
        "category_evidence_sha256": f"category-{record_id}",
        "dimension_evidence_sha256": f"dimension-{record_id}",
        "catalog_evidence_sha256": f"catalog-{record_id}",
        "provider": "lux3d",
        "model_input_hash": f"input-{record_id}",
    }


def test_catalog_content_hash_ignores_record_order_and_audit_time() -> None:
    records = [_record("2"), _record("1")]
    before = catalog_content_hash(records)
    assert before == catalog_content_hash(list(reversed(records)))
    changed = [dict(item) for item in records]
    changed[0]["material"] = "Wood"
    assert before != catalog_content_hash(changed)


def test_freeze_snapshot_is_idempotent_and_timestamp_is_not_business_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "project_test_frozen_catalog_001"
    evidence = workspace / "02_repair" / "evidence"
    evidence.mkdir(parents=True)
    (workspace / "02_repair" / "catalog_standard.xlsx").write_bytes(b"catalog")
    catalog_records = [
        {
            "record_id": "1",
            "identity_key": "cgtrader|1",
            "canonical_url": "https://www.cgtrader.com/products/1",
            "product_url": "https://www.cgtrader.com/products/1",
            "image_url": "https://img.example/1.jpg",
            "source_asset_sha256": "source-1",
            "filename_stem": "modern-black-plastic-chair-1",
            "name": "Modern Black Plastic Chair",
            "size": "18 x 20 x 32",
        }
    ]
    (evidence / "catalog_lock.json").write_text(json.dumps({
        "schema_version": 2,
        "status": "locked_partial",
        "records": catalog_records,
        "catalog": {"columns": ["name", "size", "image_url"], "content_sha256": "legacy"},
    }), encoding="utf-8")
    (workspace / "02_repair" / "model_input_locks.json").write_text(json.dumps({
        "schema_version": "workflow-locks.v1",
        "order_id": workspace.name,
        "records": [_record("1")],
    }), encoding="utf-8")

    snapshot = freeze_catalog_snapshot(workspace, order_id=workspace.name, exact_n_target=1)
    assert snapshot["schema_version"] == FROZEN_CATALOG_SCHEMA_VERSION
    assert snapshot["record_count"] == 1
    snapshot_path = workspace / "03_models" / "state" / "frozen_catalog_snapshot.json"
    mutated = json.loads(snapshot_path.read_text(encoding="utf-8"))
    mutated["frozen_at"] = "2099-01-01T00:00:00Z"
    snapshot_path.write_text(json.dumps(mutated), encoding="utf-8")
    assert read_frozen_catalog(workspace)["catalog_content_hash"] == snapshot["catalog_content_hash"]
    assert freeze_catalog_snapshot(workspace, order_id=workspace.name, exact_n_target=1)["catalog_content_hash"] == snapshot["catalog_content_hash"]


def test_generation_scope_is_singleton_for_same_business_scope() -> None:
    left = generation_scope_key(
        project_id="project_a", catalog_content_hash="a" * 64, provider="lux3d", exact_n_target=200,
    )
    right = generation_scope_key(
        project_id="project_a", catalog_content_hash="a" * 64, provider="lux3d", exact_n_target=200,
    )
    changed = generation_scope_key(
        project_id="project_a", catalog_content_hash="b" * 64, provider="lux3d", exact_n_target=200,
    )
    assert left == right
    assert left != changed
