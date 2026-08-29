from __future__ import annotations

from dataclasses import dataclass

import pytest

from packages.workflow_core.attributes import resolve_primary_attributes
from packages.workflow_core.coverage import ExactNCoverageCalculator
from packages.workflow_core.dimensions import govern_dimensions
from packages.workflow_core.naming import NamingReviewRequired, compose_product_name
from packages.workflow_core.preview import SharedPreviewSelector


def test_exact_n_coverage_counts_ready_active_and_recoverable_once() -> None:
    coverage = ExactNCoverageCalculator.from_counts(
        successful_raw=10,
        ready_unsubmitted=5,
        active_provider_tasks=2,
        recoverable_known_provider_tasks=3,
    )
    assert coverage.coverage == 20
    assert coverage.deficit(25) == 5


def test_exact_n_coverage_reserves_unresolved_submission_unknown() -> None:
    coverage = ExactNCoverageCalculator.from_counts(
        successful_raw=199,
        unresolved_submission_unknown=1,
    )
    assert coverage.coverage == 200
    assert coverage.deficit(200) == 0
    assert coverage.to_dict()["unresolved_submission_unknown"] == 1


def test_round_half_up_is_governed_for_model_input_dimensions() -> None:
    assert govern_dimensions({"width": 1.5, "depth": 2.49, "height": 2.5}) == {
        "width": 2, "depth": 2, "height": 3,
    }


def test_primary_resolver_canonicalizes_type_alias_without_inventing_vocabulary() -> None:
    resolved = resolve_primary_attributes(
        style="Modern", color="White", material="Wood", product_type="Console Table",
        category_group="Table", confidence=0.9, single_product=True,
        background_ok=True, eligible=False, image_to_3d_suitable=False,
        rejection_reason="secondary metal frame is present",
    )
    assert resolved is not None
    assert resolved.product_type == "Console"
    assert resolved.mode == "primary_attribute_rescue"


def test_primary_resolver_rejects_low_quality_or_unknown_vocabulary() -> None:
    assert resolve_primary_attributes(
        style="Modern", color="White", material="Wood", product_type="Chair",
        category_group="Chair", confidence=0.81, single_product=True,
        background_ok=True, eligible=False, image_to_3d_suitable=False,
        rejection_reason="mixed secondary material",
    ) is None
    assert resolve_primary_attributes(
        style="Invented", color="White", material="Wood", product_type="Chair",
        category_group="Chair", confidence=0.95, single_product=True,
        background_ok=True, eligible=False, image_to_3d_suitable=False,
        rejection_reason="mixed secondary material",
    ) is None


def test_naming_optional_feature_remains_governed_and_bounded() -> None:
    assert compose_product_name(
        style="Modern", color="Brown", material="Wood", feature="Upholstered", product_type="Chair",
    ) == "Modern Brown Wood Upholstered Chair"
    with pytest.raises(NamingReviewRequired):
        compose_product_name(
            style="Modern", color="Brown", material="Wood", feature="Freeform", product_type="Chair",
        )


def test_shared_preview_selector_prefers_official_non_thumbnail_media() -> None:
    ranked = SharedPreviewSelector().select([
        "https://img-new.cgtrader.com/items/1/thumb.jpg",
        "https://img-new.cgtrader.com/items/1/original.jpg",
        "https://example.invalid/not-allowed.jpg",
    ])
    assert ranked[0].url.endswith("original.jpg")
    assert len(ranked) == 2
