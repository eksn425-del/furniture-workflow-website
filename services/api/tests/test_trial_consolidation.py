"""Offline regression/protocol tests; never real-site acceptance evidence."""
import base64
import hashlib
import json
import time

import pytest
from PIL import Image

from app.services.brain_provider import BrainSettings, WebsiteBrainProvider, VisionInputRequired, _safe_trace_value
from app.services.native_contracts import BrainTaxonomyResponse
from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.site_scan_runtime import SiteScanRuntimeService as SiteScanRuntime


def test_l2_keeps_same_scan_count_without_promoting_blocker():
    first = {"source_type": "DIRECT_BRAND", "categories": [{"source_url": "https://example.test/chairs", "count_kind": "EXACT", "count_value": 86, "evidence": [{"role": "visible_count"}]}]}
    second = {"status": "PARTIAL", "source_type": "UNKNOWN", "categories": [{"source_url": "https://example.test/chairs/", "count_kind": "UNKNOWN", "count_value": None}, {"source_url": "https://example.test/tables", "count_kind": "UNKNOWN", "count_value": None}]}
    result = SiteScanRuntime._merge_enrichment(first, second)
    assert result["categories"][0]["count_value"] == 86
    assert result["categories"][1]["count_value"] is None
    assert result["source_type"] == "DIRECT_BRAND"
    assert result["status"] == "PARTIAL"
    assert second["categories"][0]["count_value"] is None
    assert SiteScanRuntime._merge_enrichment(first, {"status": "ACCESS_CHANGE_REQUIRED", "categories": []})["status"] == "ACCESS_CHANGE_REQUIRED"


def test_reviewed_department_overrides_wrapper_and_excludes_marketing():
    dept = NativeSiteAnalyzer._category("https://example.test/collections/seating", "Seating", 20, [{"role": "nav_tree", "level": 2, "parent_path": "/collections/all"}], .9)
    promo = NativeSiteAnalyzer._category("https://example.test/collections/coastal-modern", "Coastal Modern", 99, [], .8)
    brain = BrainTaxonomyResponse(categories=[
        dict(native_name="Seating", canonical_name="Seating", path="/seating", source_url=dept.source_url, level=1),
        dict(native_name="Chairs", canonical_name="Chairs", path="/chairs", source_url="https://example.test/collections/chairs", level=2, parent_path="/seating"),
    ])
    result = NativeSiteAnalyzer._merge_brain([dept, promo], brain)
    assert {c.path for c in result} == {"/seating", "/chairs"}
    assert next(c for c in result if c.path == "/seating").level == 1


def test_visual_protocol_exact_bytes_and_bridge_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path))
    path = tmp_path / "view.png"
    Image.new("RGB", (20, 20), "white").save(path)
    evidence = {"candidate_id": "c1", "media_path": str(path), "orientation_views": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
    remote = WebsiteBrainProvider(BrainSettings(model_mode="MULTIMODAL_SINGLE_MODEL"))
    bridge = WebsiteBrainProvider(BrainSettings(model_mode="CODEX_DEVELOPMENT_BRIDGE"))
    remote_messages = remote._messages("protocol test", {"evidence": evidence})
    assert remote_messages == bridge._messages("protocol test", {"evidence": evidence})
    parts = remote_messages[1]["content"]
    image = next(p for p in parts if p["type"] == "image_url")
    assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == path.read_bytes()
    trace = json.dumps(_safe_trace_value(remote_messages))
    assert "base64," not in trace and "LOCAL_VISUAL_REFERENCE" in trace
    assert "transport_sha256" in trace
    path.write_bytes(b"not an image")
    with pytest.raises(VisionInputRequired):
        remote._messages("test", {"evidence": evidence})


def test_visual_rejects_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "owned"))
    with pytest.raises(VisionInputRequired):
        WebsiteBrainProvider._local_visual_input(str(tmp_path / "private.png"))


def test_bridge_directory_is_not_connected_brain(tmp_path):
    brain = WebsiteBrainProvider(BrainSettings(model_mode="CODEX_DEVELOPMENT_BRIDGE", bridge_root=str(tmp_path)))
    assert brain.health()["operational"] is False
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    receipt = sessions / "accepted.json"
    receipt.write_text(json.dumps({"status": "RESPONDED", "updated_at": time.time(), "response_sha256": "abc"}))
    assert brain.health()["status"] == "CODEX_BRIDGE_RECENT_RESPONSE"
    receipt.write_text(json.dumps({"status": "RESPONDED", "updated_at": time.time() - 181, "response_sha256": "abc"}))
    assert brain.health()["operational"] is False


def test_scale_anchor_preserves_official_and_marks_estimate(tmp_path, monkeypatch):
    from test_domain_agent_v3 import _dimension_adapter, _candidate
    adapter, database = _dimension_adapter(tmp_path)
    try:
        monkeypatch.setattr(adapter, "_extract_dimensions_bounded", lambda *_: ({}, "in"))
        official = _candidate(dimensions={"width": 30}, decision={"height": 99, "dimension_unit": "in", "dimension_source": "AI_ESTIMATED"})
        outcome = adapter._stage_dimension(official)
        assert outcome.decision.value == "accepted"
        assert outcome.evidence["target_dimensions"] == {"width": 30}
        assert outcome.evidence["dimension_source"] == "OFFICIAL_PAGE"
        estimate = _candidate(dimensions={}, decision={"height": 18, "dimension_unit": "in", "dimension_source": "AI_ESTIMATED"})
        outcome = adapter._stage_dimension(estimate)
        assert outcome.decision.value == "accepted"
        assert outcome.evidence["target_dimensions"] == {"height": 18}
        assert outcome.evidence["dimension_source"] == "AI_ESTIMATED"
        assert outcome.evidence["dimension_lookup_contract_state"] == "LOOKUP_INCOMPLETE"
        assert outcome.evidence["dimension_lookup_state"] != "OFFICIAL_ABSENT_CONFIRMED"
    finally:
        database.dispose()


def test_visible_specification_table_units_and_diameter():
    from app.services.product_acquisition import _parse_dimension_text_structured
    result = _parse_dimension_text_structured("Diameter (cm): 12\nHeight (cm): 18", url="https://example.test/product")
    assert result["dimensions"] == {"width": 12, "depth": 12, "height": 18}
    assert result["dimension_unit"] == "cm"


def test_paused_background_does_not_touch_queue(monkeypatch):
    from app.services.production_runtime import ProductionRuntimeService
    monkeypatch.setenv("WEBSITE_BACKGROUND_WORK_PAUSED", "true")
    # No DB/executor attributes: any queue access would fail this check.
    SiteScanRuntime.__new__(SiteScanRuntime).reconcile_all()
    SiteScanRuntime.__new__(SiteScanRuntime)._schedule("scan-unchanged")
    ProductionRuntimeService.__new__(ProductionRuntimeService).reconcile_all()
    ProductionRuntimeService.__new__(ProductionRuntimeService)._promote_next()


def test_site_library_keeps_explicit_brand_setting(tmp_path):
    from types import SimpleNamespace
    from app.database import Database
    from app.models import ProductionJob, SiteRegistryRecord
    from app.api.routes.control_plane import list_control_sites
    database = Database(tmp_path / "brand.sqlite3")
    database.create_schema()
    try:
        session = database.session_factory()
        session.add(SiteRegistryRecord(site_key="brand.test", domain="brand.test", display_name="Brand"))
        session.add(ProductionJob(job_id="brand-job", site_key="brand.test", source_url="https://brand.test/", title="Brand", goal="Test", is_brand_library=True, brand_name="Original Brand"))
        session.commit()
        session.close()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=database)))
        row = list_control_sites(request)["items"][0]
        assert row["is_brand_library"] is True
        assert row["brand_name"] == "Original Brand"
    finally:
        database.dispose()
