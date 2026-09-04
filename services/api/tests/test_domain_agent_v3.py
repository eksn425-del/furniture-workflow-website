"""Focused regressions for the V3 bounded Website Domain Agent contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import Database
from app.services.brain_provider import AgentToolError, BrainSettings, WebsiteBrainProvider
from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.product_acquisition import (
    BrowserHumanRequired,
    rank_product_images,
    select_product_image,
)
from app.services.site_profile import (
    build_site_profile,
    profile_drift_reasons,
    profile_is_reusable,
    validate_site_profile,
)
from app.services.strategy_catalog import default_strategy_plan, validate_strategy_plan
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord
from workers.production_pipeline import WebsiteStageAdapter, default_provider_call_limit


class _AgentClient:
    def __init__(self, **_: object) -> None:
        self.urls: list[str] = []

    def get_html(self, url: str) -> str:
        self.urls.append(url)
        return """
        <html><head><link rel='next' href='/chairs?page=2'></head><body>
          <h1>Oak Lounge Chair</h1>
          <a href='/products/oak-chair'>Oak Lounge Chair</a>
          <script type='application/ld+json'>{"@type":"Product","name":"Oak Lounge Chair","image":"/media/oak-main.jpg"}</script>
          <p>Dimensions: W 30 in x D 32 in x H 34 in</p>
          <img src='/media/oak-lifestyle.jpg'>
        </body></html>
        """


def _candidate(*, dimensions: dict[str, float], decision: dict[str, object] | None = None) -> CandidateRecord:
    return CandidateRecord(
        candidate_id="candidate-v3",
        order_id="job-v3",
        job_id="job-v3",
        record_id="record-v3",
        source="example.test",
        source_product_id="sku-v3",
        canonical_url="https://example.test/products/chair",
        preview_id="chair",
        preview_url="https://example.test/media/chair.jpg",
        capture_sha256="capture-v3",
        image_sha256="image-v3",
        lineage={"source_dimensions": dimensions, "brain_product_decision": decision or {}},
    )


def _dimension_adapter(tmp_path: Path, *, contract: dict | None = None) -> tuple[WebsiteStageAdapter, Database]:
    database = Database(tmp_path / "v3.sqlite3")
    database.create_schema()
    pool = CandidatePoolStore(tmp_path / "pool.json", order_id="job-v3", job_id="job-v3")
    acquisition = SimpleNamespace(browser_session_dir=tmp_path / "browser")
    adapter = WebsiteStageAdapter(
        contract={
            "job_id": "job-v3",
            "source_url": "https://example.test/",
            "site_key": "example.test",
            "provider": "OFF",
            **(contract or {}),
        },
        database=database,
        pool=pool,
        acquisition=acquisition,
        workspace=tmp_path,
        brain=WebsiteBrainProvider(settings=BrainSettings()),
        provider_client=None,
        blender_adapter=None,
        media_client_factory=None,
        emit=lambda *_: None,
    )
    return adapter, database


def test_strategy_catalog_is_closed_and_platform_defaults_validate() -> None:
    for platform in ("MAGENTO", "SHOPIFY", "UNKNOWN"):
        plan = default_strategy_plan(platform).as_dict()
        assert validate_strategy_plan(plan) == (True, [])
    valid, errors = validate_strategy_plan({"taxonomy_strategy": "INVENTED"})
    assert not valid
    assert any("taxonomy_strategy" in error for error in errors)


def test_site_profile_is_safe_reusable_and_drift_detectable() -> None:
    profile = build_site_profile(
        site_key="example.test",
        source_url="https://example.test/",
        source_type="DIRECT_BRAND",
        platform="SHOPIFY",
        plan=default_strategy_plan("SHOPIFY"),
        status="VALIDATED",
        confidence=0.9,
        evidence={"public": True},
    )
    assert validate_site_profile(profile, allow_draft=False).status == "VALIDATED"
    assert profile_is_reusable(profile, site_key="example.test", platform="SHOPIFY")
    assert "PLATFORM_CHANGED" in profile_drift_reasons(profile, site_key="example.test", platform="MAGENTO")
    profile["evidence"] = {"user_data_dir": "must-not-persist"}
    with pytest.raises(ValueError, match="secret_or_session"):
        validate_site_profile(profile)
    draft_without_url = build_site_profile(
        site_key="pending.example",
        source_url="",
        source_type="UNKNOWN",
        platform="UNKNOWN",
        status="DRAFT",
    )
    assert draft_without_url["public_url_patterns"] == []


def test_image_ranking_rejects_lifestyle_and_selects_clean_product_media() -> None:
    ranked = rank_product_images(
        {"image": ["https://cdn.example.test/media/chair-lifestyle.jpg", "https://cdn.example.test/media/chair-main.jpg"]},
        page_url="https://example.test/products/chair",
    )
    assert ranked[0]["url"].endswith("chair-main.jpg")
    assert not any(item["accepted"] for item in ranked if "lifestyle" in item["url"])
    selection = select_product_image({"image": "https://cdn.example.test/media/chair-lifestyle.jpg"})
    assert selection["status"] == "NO_CLEAN_PRODUCT_IMAGE"


def test_agent_tools_are_bounded_same_site_and_emit_safe_profile(tmp_path: Path) -> None:
    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=_AgentClient)
    client = _AgentClient()
    strategy_results: dict[str, object] = {}
    profile_result: dict[str, object] = {}
    discovered = analyzer._agent_dispatch(
        "discover_products", {"url": "https://example.test/chairs", "limit": 2}, client, "https://example.test/",
        strategy_results=strategy_results,
    )
    assert discovered["bounded"] is True
    assert strategy_results["product_discovery_strategy"] in {"JSON_LD_PRODUCTS", "GENERIC_PRODUCT_CARDS"}
    with pytest.raises(AgentToolError) as error_info:
        analyzer._agent_dispatch("browse", {"url": "https://other.example/chairs"}, client, "https://example.test/")
    assert error_info.value.code == "OUT_OF_SCOPE_URL"
    analyzer._agent_dispatch("list_categories", {"url": "https://example.test/chairs"}, client, "https://example.test/", strategy_results=strategy_results)
    analyzer._agent_dispatch("inspect_product", {"url": "https://example.test/products/oak-chair"}, client, "https://example.test/", strategy_results=strategy_results)
    analyzer._agent_dispatch("inspect_pagination", {"url": "https://example.test/chairs"}, client, "https://example.test/", strategy_results=strategy_results)
    analyzer._agent_dispatch("list_product_images", {"url": "https://example.test/products/oak-chair"}, client, "https://example.test/", strategy_results=strategy_results)
    analyzer._agent_dispatch("inspect_dimensions", {"url": "https://example.test/products/oak-chair"}, client, "https://example.test/", strategy_results=strategy_results)
    selected_plan = {field: strategy_results[field] for field in (
        "taxonomy_strategy", "product_discovery_strategy", "pagination_strategy",
        "pdp_strategy", "image_strategy", "dimension_strategy",
    )}
    analyzer._agent_dispatch("validate_strategy", selected_plan, client, "https://example.test/", strategy_results=strategy_results)
    profile = analyzer._agent_dispatch(
        "finish_site_profile",
        {**selected_plan, "platform": "SHOPIFY", "source_type": "DIRECT_BRAND", "confidence": 0.8},
        client,
        "https://example.test/",
        strategy_results=strategy_results,
        profile_result=profile_result,
    )
    assert profile["schema_version"] == "website-site-profile.v1"
    assert profile_result["status"] == "VALIDATED"
    assert "user_data_dir" not in str(profile)


def test_dimensions_use_official_before_ai_and_block_on_challenge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, database = _dimension_adapter(tmp_path)
    try:
        official = _candidate(dimensions={"width": 30, "depth": 32, "height": 34}, decision={"height": 99, "dimension_source": "AI_ESTIMATED"})
        monkeypatch.setattr(adapter, "_extract_dimensions_bounded", lambda *_: pytest.fail("L2 must not run when all official axes exist"))
        outcome = adapter._stage_dimension(official)
        assert outcome.decision.value == "accepted"
        assert outcome.evidence["dimension_source"] == "OFFICIAL_PAGE"

        def challenge(*_: object, **__: object) -> tuple[dict[str, float], str]:
            raise BrowserHumanRequired("challenge", url=official.canonical_url, session_dir=tmp_path / "browser", reason_code="VISIBLE_CHALLENGE")

        blocked = _candidate(dimensions={}, decision={"height": 99, "dimension_source": "AI_ESTIMATED"})
        monkeypatch.setattr(adapter, "_extract_dimensions_bounded", challenge)
        blocked_outcome = adapter._stage_dimension(blocked)
        assert blocked_outcome.decision.value == "pending"
        assert blocked_outcome.reason == "DIMENSIONS_BROWSER_HUMAN_REQUIRED"
        assert blocked.lineage["dimension_lookup_state"] == "OFFICIAL_LOOKUP_BLOCKED"

        partial = _candidate(dimensions={"width": 30}, decision={"height": 99, "dimension_source": "AI_ESTIMATED"})
        monkeypatch.setattr(adapter, "_extract_dimensions_bounded", lambda *_: ({}, "in"))
        partial_outcome = adapter._stage_dimension(partial)
        assert partial_outcome.decision.value == "pending"
        assert partial_outcome.reason == "DIMENSIONS_OFFICIAL_LOOKUP_INCOMPLETE"
        assert partial.lineage["dimension_lookup_state"] == "OFFICIAL_LOOKUP_INCOMPLETE"
    finally:
        database.dispose()


def test_default_provider_limit_is_exact_n_and_category_aware() -> None:
    assert default_provider_call_limit({"target_mode": "EXACT_N", "target_value": 4}) == 4
    assert default_provider_call_limit({"target_mode": "EXACT_N", "target_value": 2, "category_allocation": "PER_CATEGORY", "categories": [{"selected": True}, {"selected": True}]}) == 4
    assert default_provider_call_limit({"target_mode": "ALL", "target_value": None}) == 1
