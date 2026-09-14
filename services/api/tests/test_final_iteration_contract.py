"""Final-iteration regressions for the evidence, recovery and safety gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import Database
from app.models import SiteProfile, SiteRegistryRecord, SiteScanRun
from app.services.brain_provider import BrainSettings, WebsiteBrainProvider, _safe_trace_value
from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.product_acquisition import AcquiredProduct, ProductAcquisitionEngine
from app.services.site_profile import build_site_profile
from app.services.site_scan_runtime import SiteScanRuntimeService
from workers.scrape.http_client import HttpStatusError, NetworkPolicyError, SafeHttpClient, validate_public_url
from workers.production_pipeline import WebsiteStageAdapter
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord


def _receipt(source_url: str) -> dict[str, object]:
    return {
        "status": "PARTIAL",
        "verified": False,
        "taxonomy_level": "L0",
        "source_type": "UNKNOWN",
        "source_scope": "SITE",
        "categories": [],
        "brain": {"status": "NOT_NEEDED", "provider_posts": 0},
        "evidence": {"network_called": True},
        "source_url": source_url,
    }


@pytest.mark.parametrize("profile_json", [None, "null", "", "[]", "{broken", "{}", '{"status":"DRAFT","site_key":"profile-shapes.test"}'])
def test_site_scan_profile_shapes_never_raise_unbound_local(tmp_path: Path, profile_json: str | None) -> None:
    """A new scan must survive absent, null, malformed and non-object profiles."""

    database = Database(tmp_path / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    scan_id = f"scan-{abs(hash(str(profile_json))) % 1_000_000}"
    try:
        session.add(SiteRegistryRecord(site_key="profile-shapes.test", domain="profile-shapes.test", display_name="Shapes"))
        if profile_json is not None:
            session.add(SiteProfile(
                site_key="profile-shapes.test",
                source_url="https://profile-shapes.test/",
                profile_json=profile_json,
                rules_version="website-site-profile.v1",
                status="DRAFT",
            ))
        session.add(SiteScanRun(
            scan_id=scan_id,
            site_key="profile-shapes.test",
            source_url="https://profile-shapes.test/",
            status="QUEUED",
            live=True,
        ))
        session.commit()
    finally:
        session.close()

    class Analyzer:
        @staticmethod
        def analyze(source_url: str, *, live: bool, output_dir: Path, profile=None) -> dict[str, object]:
            assert live is True
            output_dir.mkdir(parents=True, exist_ok=True)
            assert profile is None or isinstance(profile, dict)
            return _receipt(source_url)

    runtime = SiteScanRuntimeService(database, tmp_path / "output", Analyzer())
    try:
        runtime._execute(scan_id)
        state = runtime.status(scan_id)
        assert state is not None
        assert state["status"] != "FAILED"
    finally:
        runtime.shutdown()
        database.dispose()


def test_empty_agent_finish_stays_draft(tmp_path: Path) -> None:
    analyzer = NativeSiteAnalyzer(tmp_path)
    plan = {
        "taxonomy_strategy": "GENERIC_CATEGORY_LINKS",
        "product_discovery_strategy": "GENERIC_PRODUCT_CARDS",
        "pagination_strategy": "NO_VERIFIED_CONTINUATION",
        "pdp_strategy": "VISIBLE_PDP_TEXT",
        "image_strategy": "BROWSER_GALLERY",
        "dimension_strategy": "SPECIFICATION_PANEL",
    }
    strategy_results = {**plan, "strategy_validated": True, "_evidence": {}}
    profile_result: dict[str, object] = {}
    profile = analyzer._agent_dispatch(
        "finish_site_profile",
        {**plan, "platform": "UNKNOWN", "source_type": "DIRECT_BRAND", "confidence": 0.9},
        SimpleNamespace(),
        "https://empty-evidence.test/",
        strategy_results=strategy_results,
        profile_result=profile_result,
    )
    assert profile["status"] == "DRAFT"
    assert profile_result["status"] == "DRAFT"
    assert not profile["evidence"]["bounded_verification"]["capability_evidence"]["taxonomy_strategy"]


def test_dimension_axes_merge_units_without_reinterpreting_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "dimensions.sqlite3")
    database.create_schema()
    pool = CandidatePoolStore(tmp_path / "pool.json", order_id="job", job_id="job")
    adapter = WebsiteStageAdapter(
        contract={"job_id": "job", "source_url": "https://dimensions.test/", "site_key": "dimensions.test", "provider": "OFF"},
        database=database,
        pool=pool,
        acquisition=SimpleNamespace(browser_session_dir=tmp_path / "browser"),
        workspace=tmp_path,
        brain=WebsiteBrainProvider(settings=BrainSettings()),
        provider_client=None,
        blender_adapter=None,
        media_client_factory=None,
        emit=lambda *_: None,
    )
    candidate = CandidateRecord(
        candidate_id="candidate", order_id="job", job_id="job", record_id="record", source="dimensions.test",
        source_product_id="sku", canonical_url="https://dimensions.test/products/chair", preview_id="chair",
        preview_url="https://dimensions.test/media/chair.jpg", capture_sha256="capture", image_sha256="image",
        lineage={"source_dimensions": {"width": 80}, "dimension_unit": "cm", "brain_product_decision": {}},
    )
    monkeypatch.setattr(adapter, "_extract_dimensions_bounded", lambda *_: {
        "dimensions": {"depth": 60, "height": 90},
        "dimension_unit": "cm",
        "axes": {
            "depth": {"value": 60, "unit": "cm", "source": "OFFICIAL_PAGE", "evidence": [{"role": "spec"}]},
            "height": {"value": 35.5, "unit": "in", "source": "OFFICIAL_STRUCTURED", "evidence": [{"role": "api"}]},
        },
        "completed": True,
        "checked_pages": [candidate.canonical_url, candidate.canonical_url + "#specs"],
    })
    outcome = adapter._stage_dimension(candidate)
    assert outcome.decision.value == "accepted"
    assert outcome.evidence["dimension_unit"] == "in"
    assert outcome.evidence["dimension_axes"]["width"]["unit"] == "cm"
    assert outcome.evidence["dimension_axes"]["height"]["unit"] == "in"
    assert outcome.evidence["dimensions"]["width"] == 31
    assert outcome.evidence["dimensions"]["depth"] == 24
    assert outcome.evidence["dimensions"]["height"] == 36
    database.dispose()


def _product() -> AcquiredProduct:
    return AcquiredProduct(
        source_product_id="sku-recovery", canonical_url="https://pagination.test/products/chair",
        source_name="Recovery Chair", source_brand="Maker", category_id="chairs", category_group="Chairs",
        image_url="https://pagination.test/media/chair.jpg", dimensions={"width": 30.0, "depth": 32.0, "height": 34.0},
        dimension_unit="in", source_type="DIRECT_BRAND", capture_sha256="a" * 64,
        acquisition="HTTP", evidence={"public": True},
    )


def test_repeated_cursor_invokes_real_recovery_and_stays_resumable(tmp_path: Path) -> None:
    recovery_calls: list[dict[str, object]] = []
    engine = ProductAcquisitionEngine(
        source_url="https://pagination.test/chairs",
        site_key="pagination.test",
        source_type="SCOPED_CATEGORY",
        categories=[],
        workspace=tmp_path / "acquisition",
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: SimpleNamespace(),
        agent_recovery=lambda context: recovery_calls.append(context) or {
            "status": "AGENT_READY",
            "products": [_product()],
            "next_url": "https://pagination.test/chairs?page=3",
            "explicit_end": False,
        },
    )
    payload = {
        "products": {},
        "scope_cursors": {
            "scope_source|https://pagination.test/chairs": {
                "source_url": "https://pagination.test/chairs",
                "next_url": "https://pagination.test/chairs?page=2",
                "seen_page_urls": ["https://pagination.test/chairs?page=2"],
                "visited": True,
                "exhausted": False,
                "strategy": "HTML_LINK_OR_QUERY",
                "pages_fetched": 1,
            }
        },
    }
    engine._discover_more(payload, limit=1)
    assert recovery_calls and recovery_calls[0]["reason"] == "PAGINATION_UNVERIFIED"
    assert payload["discovery_status"] == "AGENT_RECOVERED"
    assert payload["exhausted_scopes"] == []
    assert len(payload["products"]) == 1


def test_robots_unavailable_is_not_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = SafeHttpClient(source_url="https://example.com/", request_budget=4, timeout=1, request_delay=0)
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: (_ for _ in ()).throw(HttpStatusError(503, retryable=True)))
    client._robots_for("https://example.com/")
    assert client.telemetry()["robots"]["https://example.com"] == "ROBOTS_UNAVAILABLE"

    denied = SafeHttpClient(source_url="https://example.com/", request_budget=4, timeout=1, request_delay=0)
    monkeypatch.setattr(denied, "_send", lambda *_args, **_kwargs: (_ for _ in ()).throw(HttpStatusError(403, retryable=False)))
    denied._robots_for("https://example.com/")
    assert denied.telemetry()["robots"]["https://example.com"] == "ROBOTS_DENIED"


def test_trace_redaction_and_public_url_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    safe = _safe_trace_value({"url": "https://example.com/p?token=secret&sku=chair", "cookie": "private"})
    assert "secret" not in json.dumps(safe)
    assert "private" not in json.dumps(safe)
    monkeypatch.setattr("workers.scrape.http_client.socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("127.0.0.1", 443))
    ])
    with pytest.raises(NetworkPolicyError, match="private"):
        validate_public_url("https://internal.example/?token=secret")


def test_bridge_mode_is_explicitly_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_MODEL_MODE", "CODEX_DEVELOPMENT_BRIDGE")
    monkeypatch.setenv("CODEX_BRIDGE_ROOT", str(tmp_path))
    settings = BrainSettings.from_environment()
    assert settings.codex_bridge_mode is True
    assert settings.configured is True
    assert settings.model_mode == "CODEX_DEVELOPMENT_BRIDGE"


def test_codex_bridge_receives_explicit_image_evidence(tmp_path: Path, monkeypatch) -> None:
    from PIL import Image
    import hashlib
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path))
    image = tmp_path / "chair.jpg"
    Image.new("RGB", (16, 16), "white").save(image)
    provider = WebsiteBrainProvider(settings=BrainSettings(
        model_mode="CODEX_DEVELOPMENT_BRIDGE",
        bridge_root=str(tmp_path),
    ))
    messages = provider._messages(
        "review",
        {
            "evidence": {
                "selected_media_url": "https://images.example/chair.jpg",
                "media_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "media_path": str(tmp_path / "chair.jpg"),
            }
        },
    )
    part = next(p for p in messages[-1]["content"] if p["type"] == "image_url")
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


# --- L1 agent evidence must survive an L2 enrichment pass -----------------
# Regression for the 45-site run: when a scan escalated from L1 to the L2
# browser pass, _merge_enrichment deep-copied the L2 receipt and only carried
# over per-category counts, silently discarding the L1 Brain agent trace. The
# result was that 158 real tool calls and every stop_code became invisible on
# exactly the hard sites that needed the agent most.


def test_merge_enrichment_preserves_l1_agent_trace() -> None:
    from app.services.site_scan_runtime import SiteScanRuntimeService

    l1_receipt = {
        "status": "PARTIAL",
        "source_type": "DIRECT_BRAND",
        "categories": [{"source_url": "https://x.test/chairs", "count_kind": "EXACT", "count_value": 7}],
        "brain": {"status": "CODEX_BRIDGE_RESPONSE", "task": "taxonomy_oneshot"},
        "agent_trace": {
            "status": "AGENT_READY",
            "stopped_reason": "TERMINAL_TOOL",
            "stop_code": "ROBOTS_DENIED",
            "turns": 2,
            "tool_calls": [{"name": "get_count", "result_status": "TERMINAL", "error_code": "ROBOTS_DENIED"}],
        },
    }
    l2_receipt = {
        "status": "PARTIAL",
        "source_type": "UNKNOWN",
        "categories": [{"source_url": "https://x.test/chairs", "count_kind": "UNKNOWN", "count_value": None}],
        "brain": {"status": "CODEX_BRIDGE_RESPONSE", "task": "access"},
        "agent_trace": {},
    }

    merged = SiteScanRuntimeService._merge_enrichment(l1_receipt, l2_receipt)

    # L2's own brain record still wins, but the L1 record stays reachable.
    assert merged["brain"]["task"] == "access"
    assert merged["brain"]["l1_brain"]["task"] == "taxonomy_oneshot"
    # The agent trace is restored rather than dropped.
    assert merged["agent_trace"]["stop_code"] == "ROBOTS_DENIED"
    assert merged["agent_trace"]["turns"] == 2
    assert merged["brain"]["agent_loop"]["tool_calls"][0]["name"] == "get_count"
    # The pre-existing count-preservation behaviour is unchanged.
    assert merged["categories"][0]["count_value"] == 7


def test_merge_enrichment_keeps_l2_agent_trace_when_present() -> None:
    from app.services.site_scan_runtime import SiteScanRuntimeService

    l1_receipt = {"categories": [], "agent_trace": {"stop_code": "MAX_STEPS"}, "brain": {"task": "taxonomy_oneshot"}}
    l2_agent = {"stop_code": "FINISH", "turns": 3}
    l2_receipt = {"categories": [], "agent_trace": l2_agent, "brain": {"task": "taxonomy_and_site_profile"}}

    merged = SiteScanRuntimeService._merge_enrichment(l1_receipt, l2_receipt)

    # A richer L2 agent run is never overwritten by the L1 one.
    assert merged["agent_trace"] == l2_agent


def test_runtime_failure_is_classified_for_the_operator() -> None:
    from workers.native_runtime import _classify_runtime_failure

    class FakeSSLError(Exception):
        pass

    reason, guidance = _classify_runtime_failure(FakeSSLError("SSL: UNEXPECTED_EOF_WHILE_READING"))
    assert reason == "TEMPORARY_FAILURE"
    assert "重试" in guidance

    reason, _ = _classify_runtime_failure(RuntimeError("robots.txt disallows URL"))
    assert reason == "ACCESS_CHANGE_REQUIRED"

    reason, _ = _classify_runtime_failure(KeyError("unexpected"))
    assert reason == "SOFTWARE_ERROR"
