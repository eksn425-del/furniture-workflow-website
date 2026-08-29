"""Website-local source identity and media-binding facts.

Room & Board exposes several identifiers for one sellable product.  They are
kept as separate evidence fields instead of being silently collapsed into one
SKU.  The helpers are source-neutral so the Website acquisition layer can
apply the same conservative rules to future direct-brand sources.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit


MEDIA_BINDING_STATUSES = frozenset({"EXACT", "COMPATIBLE", "UNKNOWN", "MISMATCH"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")
_NUMERIC_RE = re.compile(r"^\d{3,}$")
_ASSET_RE = re.compile(r"(?:^|[_/-])(\d{3,})(?:[_/.-]|$)")
_SECONDARY_JSONLD_ROLES = frozenset({"secondary", "secondary_internal", "secondary_structured", "non_authoritative", "internal_secondary"})


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split()).strip()


def extract_url_tail_id(url: Any) -> str:
    path = urlsplit(clean(url)).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return tail if _ID_RE.fullmatch(tail) and _NUMERIC_RE.fullmatch(tail) else ""


def media_asset_identity(url: Any) -> str:
    raw = unquote(clean(url))
    if not raw:
        return ""
    query = dict(parse_qsl(urlsplit(raw).query, keep_blank_values=True))
    for value in (query.get("asset_identity"), query.get("src"), urlsplit(raw).path.rsplit("/", 1)[-1]):
        match = _ASSET_RE.search(clean(value))
        if match:
            return match.group(1)
    return ""


def _jsonld_sku(evidence: Mapping[str, Any]) -> str:
    for key in ("jsonld_sku", "json_ld_sku", "sku", "jsonld_product_sku"):
        value = clean(evidence.get(key))
        if value:
            return value
    product = evidence.get("jsonld_product") or evidence.get("json_ld_product")
    if isinstance(product, Mapping):
        return clean(product.get("sku") or product.get("mpn") or product.get("productID"))
    return ""


def _jsonld_sku_role(evidence: Mapping[str, Any], *, page_item_number: str, jsonld_sku: str) -> str:
    explicit = clean(evidence.get("jsonld_sku_role")).casefold()
    if explicit:
        return explicit
    if (
        clean(evidence.get("schema_version")) == "roomandboard-browser-capture.v3"
        and clean(evidence.get("identity_status")).upper() == "VERIFIED"
        and evidence.get("jsonld_product_match") is True
        and evidence.get("jsonld_image_bound") is True
        and clean(evidence.get("item_number")) == page_item_number
        and page_item_number
        and jsonld_sku
        and page_item_number.casefold() != jsonld_sku.casefold()
    ):
        return "secondary_internal"
    return ""


def normalize_identity_fields(evidence: Mapping[str, Any]) -> dict[str, Any]:
    canonical_url = clean(evidence.get("canonical_url") or evidence.get("source_url") or evidence.get("url"))
    url_tail_id = clean(evidence.get("url_tail_id")) or extract_url_tail_id(canonical_url)
    page_item_number = clean(
        evidence.get("page_item_number")
        or evidence.get("page_item")
        or evidence.get("item_number")
        or evidence.get("itemNumber")
    )
    jsonld_sku = _jsonld_sku(evidence)
    jsonld_sku_role = _jsonld_sku_role(evidence, page_item_number=page_item_number, jsonld_sku=jsonld_sku)
    product_family_name = clean(
        evidence.get("product_family_name")
        or evidence.get("product_family")
        or evidence.get("collection_identity")
        or evidence.get("product_title")
        or evidence.get("product_name")
    )
    configuration_key = clean(
        evidence.get("configuration_key")
        or evidence.get("configuration_id")
        or evidence.get("configuration_label")
    )
    variant_key = clean(
        evidence.get("variant_key")
        or evidence.get("geometry_variant_key")
        or evidence.get("variant_context")
    )
    asset_identity = clean(evidence.get("asset_identity") or evidence.get("media_asset_identity"))
    if not asset_identity:
        asset_identity = media_asset_identity(evidence.get("media_url") or evidence.get("image_url") or evidence.get("url"))
    conflicts: list[str] = []
    if url_tail_id and page_item_number and url_tail_id.casefold() != page_item_number.casefold():
        conflicts.append("url_tail_id_page_item_number_conflict")
    if page_item_number and jsonld_sku and page_item_number.casefold() != jsonld_sku.casefold() and jsonld_sku_role not in _SECONDARY_JSONLD_ROLES:
        conflicts.append("page_item_number_jsonld_sku_conflict")
    return {
        "canonical_url": canonical_url,
        "route_id": clean(evidence.get("route_id") or evidence.get("route")),
        "url_tail_id": url_tail_id,
        "page_item_number": page_item_number,
        "jsonld_sku": jsonld_sku,
        "jsonld_sku_role": jsonld_sku_role,
        "product_family_name": product_family_name,
        "configuration_key": configuration_key,
        "variant_key": variant_key,
        "asset_identity": asset_identity,
        "identity_conflicts": conflicts,
    }


def media_binding_status(
    evidence: Mapping[str, Any],
    *,
    media_url: Any = "",
    source: Any = "",
    role: Any = "",
) -> tuple[str, float, list[str]]:
    """Return ``(status, confidence, reasons)`` without guessing identity."""

    explicit = clean(evidence.get("media_binding_status")).upper()
    if explicit in MEDIA_BINDING_STATUSES:
        try:
            confidence = float(evidence.get("media_binding_confidence") or (1.0 if explicit == "EXACT" else 0.9 if explicit == "COMPATIBLE" else 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return explicit, confidence, list(evidence.get("media_binding_reasons") or [])

    identity = normalize_identity_fields(evidence)
    asset = clean(evidence.get("asset_identity")) or media_asset_identity(media_url)
    expected = [identity[key] for key in ("page_item_number", "jsonld_sku") if identity[key]]
    reasons = list(identity["identity_conflicts"])
    if identity["identity_conflicts"]:
        return "MISMATCH", 0.0, reasons
    source_token = clean(source).casefold()
    bound = bool(
        evidence.get("product_identity_match")
        or evidence.get("jsonld_image_bound")
        or evidence.get("gallery_bound_to_product")
        or evidence.get("bound_to_product")
    )
    structured_sources = {
        "json_ld_image", "json_ld_product", "jsonld_product", "product_gallery",
        "product_gallery_metadata", "browser_gallery_dom", "official_page_evidence",
    }
    if bound and source_token in structured_sources:
        if asset and expected and any(asset.casefold() == value.casefold() for value in expected):
            return "EXACT", 1.0, ["asset_identity_matches_page_product"]
        return "COMPATIBLE", 0.9, ["structured_media_bound_to_current_product"]
    if asset and expected:
        if any(asset.casefold() == value.casefold() for value in expected):
            return "EXACT", 1.0, ["asset_identity_matches_page_product"]
        return "MISMATCH", 0.0, ["asset_identity_does_not_match_page_product"]
    if bound and source_token in structured_sources:
        return "COMPATIBLE", 0.9, ["structured_media_bound_to_current_product"]
    return "UNKNOWN", 0.0, ["media_asset_identity_not_proven"]


def scope_status(*values: Any) -> tuple[str, list[str]]:
    text = " ".join(clean(value) for value in values if clean(value)).casefold()
    if not text:
        return "UNKNOWN", ["scope_evidence_missing"]
    if re.search(r"\bbedroom\b", text) and re.search(r"\bvanity\b", text):
        return "CONFLICT", ["bedroom_vanity_scope_conflict"]
    if re.search(r"\b(?:room scene|room setting|lifestyle|inspiration)\b", text):
        return "CONFLICT", ["room_scene_scope_conflict"]
    return "PASS", []


def needs_l2_browser(
    *,
    media_status: str = "",
    binding_status: str = "",
    page_access_status: str = "",
    jsonld_ambiguous: bool = False,
    dynamic_configuration: bool = False,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if binding_status.upper() == "UNKNOWN":
        reasons.append("media_binding_unknown")
    if page_access_status.casefold() in {"page_blocked", "blocked", "403", "placeholder"}:
        reasons.append("page_access_or_placeholder")
    if jsonld_ambiguous:
        reasons.append("jsonld_product_ambiguous")
    if dynamic_configuration:
        reasons.append("dynamic_configuration")
    return bool(reasons), reasons


__all__ = [
    "MEDIA_BINDING_STATUSES",
    "clean",
    "extract_url_tail_id",
    "media_asset_identity",
    "media_binding_status",
    "needs_l2_browser",
    "normalize_identity_fields",
    "scope_status",
]
