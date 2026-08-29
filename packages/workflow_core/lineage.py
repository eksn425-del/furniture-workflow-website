"""Lineage-aware delivery validation; paths alone are never evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DeliveryItem:
    job_id: str
    record_id: str
    catalog_lock_sha256: str
    source_asset_sha256: str
    model_sha256: str
    qa_status: str
    relative_path: str


class LineageError(ValueError):
    pass


def validate_delivery(
    items: Iterable[DeliveryItem],
    *,
    job_id: str,
    catalog_lock_sha256: str,
    expected_count: int,
) -> list[DeliveryItem]:
    values = list(items)
    if len(values) != expected_count:
        raise LineageError(f"delivery count {len(values)} != requested {expected_count}")
    if not values:
        raise LineageError("delivery must contain at least one item")
    record_ids = [item.record_id for item in values]
    if len(set(record_ids)) != len(record_ids):
        raise LineageError("duplicate record_id in delivery")
    model_hashes = [item.model_sha256 for item in values]
    if len(set(model_hashes)) != len(model_hashes):
        raise LineageError("duplicate model sha256 in delivery")
    for item in values:
        if item.job_id != job_id:
            raise LineageError(f"lineage job mismatch for {item.record_id}")
        if item.catalog_lock_sha256 != catalog_lock_sha256:
            raise LineageError(f"catalog lock mismatch for {item.record_id}")
        if item.qa_status != "PASSED":
            raise LineageError(f"QA not passed for {item.record_id}")
        if not item.source_asset_sha256 or not item.model_sha256 or not item.relative_path:
            raise LineageError(f"incomplete lineage for {item.record_id}")
    return values
