from __future__ import annotations

from pathlib import Path

from packages.workflow_core.naming import compose_brand_official_name, compose_official_name, compose_product_name
from packages.workflow_core.production_gate import (
    L2_BROWSER_REQUIRED,
    MEDIA_IDENTITY_MISMATCH,
    READY_FOR_MODELING,
    evaluate_production_gate,
)
from packages.workflow_core.source_identity import media_binding_status, normalize_identity_fields
from packages.workflow_core.source_policy import resolve_source_policy


def test_source_policy_contract_is_self_contained() -> None:
    contract_path = Path(__file__).resolve().parents[1] / "source_policy.v1.json"
    assert contract_path.is_file()
    website = contract_path.read_text(encoding="utf-8")
    assert '"source_profiles"' in website
    assert resolve_source_policy("https://www.cgtrader.com/furniture/chairs").is_visual_first
    assert resolve_source_policy("https://www.roomandboard.com/catalog/575954").brand_display_name == "Room & Board"


def test_identity_ids_stay_separate_and_layered_media_can_be_compatible() -> None:
    identity = normalize_identity_fields({
        "canonical_url": "https://www.roomandboard.com/catalog/575954",
        "page_item_number": "575954",
        "jsonld_sku": "575954",
        "product_family_name": "Wyatt",
        "configuration_key": "bed-queen",
        "variant_key": "queen",
        "image_url": "https://scene7.example/126154.jpg?layer=bed-queen",
    })
    assert identity["url_tail_id"] == "575954"
    assert identity["page_item_number"] == "575954"
    assert identity["jsonld_sku"] == "575954"
    assert identity["asset_identity"] == "126154"
    status, confidence, reasons = media_binding_status(
        {**identity, "product_identity_match": True, "jsonld_image_bound": True, "configuration_bound": True},
        media_url="https://scene7.example/126154.jpg?layer=bed-queen",
        source="json_ld_image",
    )
    assert (status, confidence) == ("COMPATIBLE", 0.9)
    assert reasons == ["structured_media_bound_to_current_product"]


def test_roomandboard_page_item_can_authoritatively_bind_a_secondary_jsonld_sku() -> None:
    identity = normalize_identity_fields({
        "schema_version": "roomandboard-browser-capture.v3",
        "canonical_url": "https://www.roomandboard.com/catalog/bath/vanities/berkeley-bathroom-vanities",
        "page_item_number": "690601",
        "item_number": "690601",
        "jsonld_sku": "28400",
        "jsonld_sku_role": "secondary_internal",
        "identity_status": "VERIFIED",
        "jsonld_product_match": True,
        "jsonld_image_bound": True,
    })
    assert identity["identity_conflicts"] == []
    assert identity["jsonld_sku_role"] == "secondary_internal"


def test_production_gate_is_strict_for_mismatch_and_unknown_binding() -> None:
    facts = {
        "identity": "roomandboard|575954|https://www.roomandboard.com/catalog/575954",
        "dedup_status": "UNIQUE",
        "claim_status": "CAPTURED",
        "media_binding_status": "EXACT",
        "media_binding_confidence": 1.0,
        "image_decodable": True,
        "visual_review_status": "PASS",
        "visual_confidence": 0.95,
        "source_image_vision_consistent": True,
        "scope_status": "PASS",
        "final_name": "Room & Board Wyatt Bed",
        "dimensions": {"width": 80, "depth": 84, "height": 45},
        "provider_idempotency_key": "idempotency-key",
    }
    assert evaluate_production_gate(facts, source="roomandboard.com").status == READY_FOR_MODELING
    unknown = dict(facts, media_binding_status="UNKNOWN", media_binding_confidence=0)
    assert evaluate_production_gate(unknown, source="roomandboard.com").status == L2_BROWSER_REQUIRED
    mismatch = dict(facts, media_binding_status="MISMATCH")
    assert evaluate_production_gate(mismatch, source="roomandboard.com").status == MEDIA_IDENTITY_MISMATCH


def test_all_governed_names_keep_whole_words_and_fifty_character_limit() -> None:
    marketplace = compose_product_name(
        style="Modern", color="Red", material="Fabric", feature="Tufted",
        product_type="Armchair", contract_version="naming-contract.v3-marketplace",
    )
    official = compose_official_name(
        source_name="Wyatt Collection Limited Edition Upholstered Bedroom Bed",
        verified_type="Bed",
        brand="Room & Board",
    )
    assert len(marketplace) <= 50
    assert len(official) <= 50
    assert not marketplace.endswith(" ")
    assert not official.endswith(" ")
    assert official.startswith("Room & Board ")
    assert official.endswith(" Bed")


def test_brand_official_name_does_not_duplicate_brand_and_keeps_limit() -> None:
    name = compose_brand_official_name(
        brand="Interior Define",
        official_name="Interior Define Sloan Custom Modular Sectional",
        style="Modern",
        color="Natural",
        material="Fabric",
        product_type="Sectional",
    )
    assert name.startswith("Interior Define ")
    assert name.casefold().count("interior define") == 1
    assert len(name) <= 50
