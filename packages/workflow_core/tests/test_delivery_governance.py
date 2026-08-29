from decimal import Decimal

import pytest

from packages.workflow_core.delivery_governance import (
    MAX_FILENAME_STEM_CHARS,
    allocate_filename_stems,
    governed_dimension_payload,
    governed_public_name,
)


def test_existing_shared_core_name_is_kept() -> None:
    assert governed_public_name("Modern Brown Wood Coffee Table") == "Modern Brown Wood Coffee Table"


@pytest.mark.parametrize(
    "name",
    [
        "Modern Brown Wood CGTrader Table",
        "Modern Brown Wood Table 30 x 20 x 18",
        "Modern Brown Wood Free Table",
        "Modern Brown Wood 3D Model Table",
    ],
)
def test_public_name_rejects_source_and_dimension_tokens(name: str) -> None:
    with pytest.raises(Exception):
        governed_public_name(name)


def test_round_half_up_preserves_source_and_governs_integers() -> None:
    payload = governed_dimension_payload(
        {"source_width": 29.5, "source_depth": 29.4, "source_height": 30.5}
    )
    assert payload["source_wdh"] == {
        "width": 29.5, "depth": 29.4, "height": 30.5, "unit": "inch",
    }
    assert payload["governed_wdh"] == {
        "width": 30, "depth": 29, "height": 31,
        "unit": "inch", "rounding": "ROUND_HALF_UP",
    }
    assert Decimal(str(payload["governed_wdh_m"]["width"])) == Decimal("0.762")


def test_filename_collisions_use_deterministic_suffix_and_max60() -> None:
    rows = [
        {"record_id": "b", "product_name": "Modern Brown Wood Table"},
        {"record_id": "a", "product_name": "Modern Brown Wood Table"},
        {"record_id": "c", "product_name": "Modern Brown Wood Table"},
    ]
    result = allocate_filename_stems(rows)
    assert result == {
        "a": "Modern Brown Wood Table",
        "b": "Modern Brown Wood Table 02",
        "c": "Modern Brown Wood Table 03",
    }
    assert all(len(value) <= MAX_FILENAME_STEM_CHARS for value in result.values())
