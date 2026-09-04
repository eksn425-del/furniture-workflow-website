"""Controlled, data-driven Website strategy catalog.

The Domain Agent may choose one of these reviewed strategies, but it may not
invent executable fetch code.  Keeping the catalog as plain data gives the
Website a stable extension point for new commerce families without growing a
host-by-host ``if`` tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


STRATEGY_CATALOG: dict[str, tuple[str, ...]] = {
    "taxonomy": (
        "NAV_TREE",
        "JSON_LD_COLLECTION",
        "SITEMAP",
        "MAGENTO_GRAPHQL",
        "SHOPIFY_NAV",
        "GENERIC_CATEGORY_LINKS",
        "BROWSER_NAV",
    ),
    "product_discovery": (
        "JSON_LD_PRODUCTS",
        "SHOPIFY_COLLECTION_JSON",
        "MAGENTO_GRAPHQL_PRODUCTS",
        "GENERIC_PRODUCT_CARDS",
        "SITEMAP_PRODUCTS",
        "BROWSER_PRODUCT_CARDS",
    ),
    "pagination": (
        "REL_NEXT",
        "NEXT_LINK",
        "PAGE_PARAM",
        "MAGENTO_CURRENT_PAGE",
        "SHOPIFY_PAGE",
        "LOAD_MORE",
        "BOUNDED_SCROLL",
        "NO_VERIFIED_CONTINUATION",
    ),
    "pdp": (
        "JSON_LD_PRODUCT",
        "VISIBLE_PDP_TEXT",
        "STRUCTURED_PUBLIC_API",
        "BROWSER_PDP",
    ),
    "image": (
        "JSON_LD_IMAGE",
        "OG_IMAGE",
        "PRODUCT_GALLERY",
        "STRUCTURED_MEDIA_API",
        "BROWSER_GALLERY",
    ),
    "dimensions": (
        "STRUCTURED_DIMENSIONS",
        "PDP_DIMENSION_TEXT",
        "SPECIFICATION_PANEL",
        "PUBLIC_API_DIMENSIONS",
        "BROWSER_DIMENSION_PANEL",
    ),
}

STRATEGY_FIELDS = tuple(STRATEGY_CATALOG)
ALL_STRATEGIES = frozenset(value for values in STRATEGY_CATALOG.values() for value in values)
_FIELD_TO_CATALOG = {"dimension_strategy": "dimensions"}


class UnsupportedStrategyRequired(ValueError):
    """Raised when no reviewed strategy can satisfy a bounded operation."""

    code = "UNSUPPORTED_STRATEGY_REQUIRED"


@dataclass(frozen=True, slots=True)
class StrategyPlan:
    taxonomy_strategy: str
    product_discovery_strategy: str
    pagination_strategy: str
    pdp_strategy: str
    image_strategy: str
    dimension_strategy: str

    def as_dict(self) -> dict[str, str]:
        return {
            "taxonomy_strategy": self.taxonomy_strategy,
            "product_discovery_strategy": self.product_discovery_strategy,
            "pagination_strategy": self.pagination_strategy,
            "pdp_strategy": self.pdp_strategy,
            "image_strategy": self.image_strategy,
            "dimension_strategy": self.dimension_strategy,
        }

    def validate(self) -> "StrategyPlan":
        for field, value in self.as_dict().items():
            catalog_key = _FIELD_TO_CATALOG.get(field, field.removesuffix("_strategy"))
            allowed = STRATEGY_CATALOG.get(catalog_key, ())
            if value not in allowed:
                raise UnsupportedStrategyRequired(f"{field}:{value}")
        return self


def default_strategy_plan(platform: str = "UNKNOWN") -> StrategyPlan:
    """Return the safest first plan for a detected platform.

    Platform detection is a signal, not a host-specific production branch.
    The generic plan intentionally stays usable when the platform is unknown.
    """

    normalized = str(platform or "UNKNOWN").strip().upper()
    if normalized in {"MAGENTO", "MAGENTO_PWA"}:
        return StrategyPlan(
            "MAGENTO_GRAPHQL", "MAGENTO_GRAPHQL_PRODUCTS", "MAGENTO_CURRENT_PAGE",
            "STRUCTURED_PUBLIC_API", "STRUCTURED_MEDIA_API", "PUBLIC_API_DIMENSIONS",
        )
    if normalized == "SHOPIFY":
        return StrategyPlan(
            "SHOPIFY_NAV", "SHOPIFY_COLLECTION_JSON", "SHOPIFY_PAGE",
            "JSON_LD_PRODUCT", "PRODUCT_GALLERY", "PDP_DIMENSION_TEXT",
        )
    if normalized in {"NEXT", "WOOCOMMERCE"}:
        return StrategyPlan(
            "NAV_TREE", "JSON_LD_PRODUCTS", "NEXT_LINK",
            "JSON_LD_PRODUCT", "PRODUCT_GALLERY", "PDP_DIMENSION_TEXT",
        )
    return StrategyPlan(
        "GENERIC_CATEGORY_LINKS", "GENERIC_PRODUCT_CARDS", "NO_VERIFIED_CONTINUATION",
        "VISIBLE_PDP_TEXT", "PRODUCT_GALLERY", "SPECIFICATION_PANEL",
    )


def validate_strategy_plan(payload: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate an agent/profile selection without executing anything."""

    errors: list[str] = []
    for field in (
        "taxonomy_strategy", "product_discovery_strategy", "pagination_strategy",
        "pdp_strategy", "image_strategy", "dimension_strategy",
    ):
        value = str(payload.get(field) or "").strip().upper()
        catalog_key = _FIELD_TO_CATALOG.get(field, field.removesuffix("_strategy"))
        if value not in STRATEGY_CATALOG.get(catalog_key, ()):
            errors.append(f"{field}:{value or 'MISSING'}")
    return not errors, errors


def strategy_catalog_payload() -> dict[str, list[str]]:
    """JSON-safe read model for diagnostics and agent prompts."""

    return {key: list(values) for key, values in STRATEGY_CATALOG.items()}


__all__ = [
    "ALL_STRATEGIES", "STRATEGY_CATALOG", "STRATEGY_FIELDS", "StrategyPlan",
    "UnsupportedStrategyRequired", "default_strategy_plan", "strategy_catalog_payload",
    "validate_strategy_plan",
]
