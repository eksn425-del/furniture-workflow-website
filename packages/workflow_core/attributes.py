"""Primary furniture attribute resolution for semantic rescue.

The resolver never invents vocabulary.  It only canonicalizes a value already
returned by a visual/semantic producer and permits a policy rescue when the
producer has a clear primary attribute despite secondary construction parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .naming import canonicalize_attribute


TYPE_ALIASES = {
    "armchair": "Accent Chair",
    "office chair": "Chair",
    "lounge chair": "Accent Chair",
    "recliner": "Chair",
    "console table": "Console",
    "end table": "Side Table",
    "bookshelf": "Bookcase",
    "book shelf": "Bookcase",
    "credenza": "Sideboard",
    "cupboard": "Cabinet",
    "floor lamp": "Floor Lamp",
    "table lamp": "Table Lamp",
    "pendant": "Pendant Light",
    "pendant light": "Pendant Light",
    "wall sconce": "Wall Light",
    "carpet": "Rug",
    "wall artwork": "Wall Art",
}

ALLOWED_CATEGORY_GROUPS = {
    "Chair", "Stool/Bench", "Table", "Storage", "Sofa", "Bed", "Lighting",
    "Home Decor", "Wall Art", "Sculpture", "Rug", "Mirror", "Outdoor Furniture",
    "Kitchen/Dining", "Bathroom Fixture", "Other Furniture",
}


@dataclass(frozen=True, slots=True)
class PrimaryAttributeResolution:
    style: str
    color: str
    material: str
    product_type: str
    mode: str
    reason: str


def _type_value(value: object) -> object:
    text = str(value or "").strip()
    return TYPE_ALIASES.get(text.casefold(), text)


def resolve_primary_attributes(
    *,
    style: object,
    color: object,
    material: object,
    product_type: object,
    category_group: object,
    confidence: object,
    single_product: bool,
    background_ok: bool,
    eligible: bool,
    image_to_3d_suitable: bool,
    rejection_reason: str = "",
) -> PrimaryAttributeResolution | None:
    """Resolve governed attributes or return ``None`` for human review.

    A strict accepted reading is returned in ``strict`` mode.  A rejected
    reading can be rescued only when the rejection is a policy-level
    mixed-attribute issue, not an identity, quality, category, or background
    failure.  The host reading's single primary attributes remain the sole
    source of the resulting name.
    """

    try:
        score = float(confidence)
    except (TypeError, ValueError):
        return None
    if not single_product or not background_ok or str(category_group or "") not in ALLOWED_CATEGORY_GROUPS:
        return None
    try:
        resolved = {
            "style": canonicalize_attribute(style, "style"),
            "color": canonicalize_attribute(color, "color"),
            "material": canonicalize_attribute(material, "material"),
            "product_type": canonicalize_attribute(_type_value(product_type), "type"),
        }
    except ValueError:
        return None
    if eligible and image_to_3d_suitable and score >= 0.72:
        return PrimaryAttributeResolution(**resolved, mode="strict", reason="strict_semantic_accept")
    if score < 0.82:
        return None
    reason = str(rejection_reason or "").casefold()
    hard_markers = (
        "identity", "accessory", "non-furniture", "low-poly", "low-detail",
        "overexposed", "untextured", "unsupported default", "background",
        "outside the current", "broken wooden crate", "category", "image is severely",
    )
    if any(marker in reason for marker in hard_markers):
        return None
    return PrimaryAttributeResolution(
        **resolved,
        mode="primary_attribute_rescue",
        reason="primary_attribute_clear_despite_secondary_construction",
    )


__all__ = [
    "ALLOWED_CATEGORY_GROUPS", "PrimaryAttributeResolution", "TYPE_ALIASES",
    "resolve_primary_attributes",
]
