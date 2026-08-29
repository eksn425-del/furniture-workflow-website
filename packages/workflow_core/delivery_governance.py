"""Deterministic public-name, filename, and integer-dimension delivery rules.

This module is deliberately geometry-free.  It turns an immutable Golden
manifest row into delivery metadata without modifying the source evidence or
raw GLB.  Website and Blender delivery scripts therefore share the same
governed values and cannot drift at the hand-off boundary.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from .dimensions import govern_dimensions
from .naming import (
    NAMING_CONTRACT_V3_MARKETPLACE,
    NamingReviewRequired,
    shorten_name_to_limit,
    validate_official_name,
    validate_product_name,
)
from .source_profiles import CGTRADER_PROFILE, ROOM_AND_BOARD_PROFILE
from .source_policy import MAX_FINAL_NAME_CHARS


MAX_PUBLIC_NAME_CHARS = MAX_FINAL_NAME_CHARS
MAX_FILENAME_STEM_CHARS = MAX_FINAL_NAME_CHARS
INCH_TO_M = Decimal("0.0254")
PROHIBITED_PUBLIC_NAME = re.compile(
    r"(?:\bcgtrader\b|\b3d\s*model\b|\bfree\b|"
    r"\b(?:sku|model)\s*[-#:]*\s*[a-z0-9-]+\b|"
    r"\d+(?:\.\d+)?\s*(?:inches?|in|cm|mm|m|w|d|h)\b|"
    r"\d+(?:\.\d+)?\s*[x×]\s*\d+)",
    re.IGNORECASE,
)
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _name_profile(value: object):
    """Pick a source profile by scanning the public name for a known brand prefix."""

    folded = str(value or "").casefold()
    if folded.startswith(ROOM_AND_BOARD_PROFILE.brand_display_name.casefold()):
        return ROOM_AND_BOARD_PROFILE
    if CGTRADER_PROFILE.brand_display_name and folded.startswith(CGTRADER_PROFILE.brand_display_name.casefold()):
        return CGTRADER_PROFILE
    return None


def governed_public_name(value: object) -> str:
    """Return one already-governed public name or raise a review error.

    Names that carry a brand-direct prefix (e.g. "Room and Board") are validated
    under that source profile so the prefix is accepted and still checked for
    prohibited tokens. Dimension tokens are never allowed.
    """

    name = re.sub(r"\s+", " ", str(value or "").strip())
    profile = _name_profile(name)
    try:
        if profile is ROOM_AND_BOARD_PROFILE:
            validate_official_name(name, brand=profile.brand_display_name)
        elif profile is not None:
            validate_product_name(name, **profile.naming_kwargs())
        else:
            try:
                validate_product_name(name)
            except NamingReviewRequired:
                # Marketplace v3 is the only non-brand contract that permits
                # an explicit governed feature in the public name.
                validate_product_name(name, contract_version=NAMING_CONTRACT_V3_MARKETPLACE)
    except NamingReviewRequired as error:
        raise ValueError(str(error)) from error
    if len(name) > MAX_PUBLIC_NAME_CHARS:
        raise ValueError("public_name_exceeds_50_characters")
    if PROHIBITED_PUBLIC_NAME.search(name):
        raise ValueError("public_name_contains_prohibited_source_or_dimension_token")
    return name


def _safe_stem(value: str) -> str:
    value = re.sub(r'[\\/*?:"<>|]', "_", value).strip(" ._")
    if value.upper() in WINDOWS_RESERVED:
        value = "_" + value
    if not value:
        raise ValueError("filename_stem_empty")
    return value


def _fit_with_suffix(base: str, suffix: str) -> str:
    budget = MAX_FILENAME_STEM_CHARS - len(suffix)
    if budget <= 0:
        raise ValueError("filename_suffix_exceeds_budget")
    if len(base) > budget:
        base = shorten_name_to_limit(base, max_chars=budget)
    result = _safe_stem(base + suffix)
    if len(result) > MAX_FILENAME_STEM_CHARS:
        raise ValueError("filename_stem_exceeds_50_characters")
    return result


def allocate_filename_stems(rows: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Allocate deterministic `` 01/ 02`` collision suffixes by record identity.
    Filenames are the governed public name only; dimensions are never appended."""

    normalized = []
    for row in rows:
        record_id = str(row.get("record_id") or "").strip()
        if not record_id:
            raise ValueError("record_id_missing")
        normalized.append((governed_public_name(row.get("product_name")), record_id))
    normalized.sort(key=lambda pair: (pair[0].casefold(), pair[1].casefold()))
    ordinals: dict[str, int] = {}
    output: dict[str, str] = {}
    used: set[str] = set()
    for name, record_id in normalized:
        key = name.casefold()
        ordinal = ordinals.get(key, 0) + 1
        ordinals[key] = ordinal
        suffix = "" if ordinal == 1 else f" {ordinal:02d}"
        stem = _fit_with_suffix(_safe_stem(name), suffix)
        if stem.casefold() in used:
            raise ValueError(f"filename_collision_after_governance:{stem}")
        used.add(stem.casefold())
        output[record_id] = stem
    return output


def governed_dimension_payload(row: Mapping[str, object]) -> dict[str, object]:
    source = {
        "width": row.get("source_width"),
        "depth": row.get("source_depth"),
        "height": row.get("source_height"),
    }
    governed = govern_dimensions(source)
    meters = {
        axis: float(Decimal(value) * INCH_TO_M)
        for axis, value in governed.items()
    }
    return {
        "source_wdh": {**source, "unit": "inch"},
        "governed_wdh": {**governed, "unit": "inch", "rounding": "ROUND_HALF_UP"},
        "governed_wdh_m": {**meters, "unit": "meter"},
    }


def safe_relative_path(root: Path, value: object) -> Path:
    path = (root / str(value or "")).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ValueError(f"path_escapes_golden_root:{value}")
    return path


__all__ = [
    "INCH_TO_M",
    "MAX_FILENAME_STEM_CHARS",
    "MAX_PUBLIC_NAME_CHARS",
    "allocate_filename_stems",
    "governed_dimension_payload",
    "governed_public_name",
    "safe_relative_path",
]
