from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import Database
from app.services.brain_provider import BrainSettings, WebsiteBrainProvider
from app.services.native_contracts import BrainProductDecision
from app.services.runtime_diagnostics import collect_runtime_diagnostics
from workers.blender_adapter import BlenderCLIAdapter, ModelDimensionConflict, extract_glb_bbox, plan_dimension_normalization


def _glb_with_position_bounds(path: Path, minimum: tuple[float, float, float], maximum: tuple[float, float, float]) -> None:
    document = {
        "asset": {"version": "2.0"},
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"componentType": 5126, "count": 8, "type": "VEC3", "min": list(minimum), "max": list(maximum)}],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total = 12 + 8 + len(payload)
    path.write_bytes(
        b"glTF"
        + (2).to_bytes(4, "little")
        + total.to_bytes(4, "little")
        + len(payload).to_bytes(4, "little")
        + b"JSON"
        + payload
    )


def test_local_checkout_defaults_to_local_agent_without_brain_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LOCAL_REVIEW_MODE", "WEBSITE_MODEL_MODE", "LUNAMAX_MODEL_MODE", "WEBSITE_BRAIN_API_KEY", "WEBSITE_BRAIN_BASE_URL", "WEBSITE_BRAIN_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = BrainSettings.from_environment()
    assert settings.model_mode == "LOCAL_AGENT"
    assert settings.mode_source == "local_default"
    assert WebsiteBrainProvider(settings).health()["status"] == "LOCAL_AGENT_READY"


def test_explicit_remote_mode_without_credentials_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_REVIEW_MODE", raising=False)
    monkeypatch.setenv("WEBSITE_MODEL_MODE", "TEXT_BRAIN_PLUS_VISION")
    monkeypatch.delenv("WEBSITE_BRAIN_API_KEY", raising=False)
    monkeypatch.delenv("WEBSITE_BRAIN_BASE_URL", raising=False)
    monkeypatch.delenv("WEBSITE_BRAIN_MODEL", raising=False)
    settings = BrainSettings.from_environment()
    assert settings.model_mode == "TEXT_BRAIN_PLUS_VISION"
    assert not settings.local_agent_mode
    assert WebsiteBrainProvider(settings).health()["status"] == "BRAIN_NOT_CONFIGURED"


def test_runtime_diagnostics_is_non_secret_and_reports_effective_capabilities(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WEBSITE_L2_BROWSER_ENGINE", "unsupported-engine")
    monkeypatch.setenv("PROVIDER_MODE", "disabled")
    monkeypatch.delenv("LUX3D_API_KEY", raising=False)
    monkeypatch.delenv("LUX3D_BASE_URL", raising=False)
    monkeypatch.delenv("BLENDER_WORKER_ENABLED", raising=False)
    database = Database(tmp_path / "control.sqlite3")
    database.create_schema()
    try:
        diagnostics = collect_runtime_diagnostics(database, WebsiteBrainProvider(BrainSettings(model_mode="LOCAL_AGENT")))
        assert diagnostics["api"]["status"] == "READY"
        assert diagnostics["database"]["status"] == "READY"
        assert diagnostics["brain"]["effective_mode"] == "LOCAL_AGENT"
        assert diagnostics["lux3d"]["status"] == "OFF"
        assert diagnostics["blender"]["status"] == "NOT_CONFIGURED"
        assert diagnostics["l2_browser"]["status"] == "NOT_INSTALLED"
        assert "path" not in json.dumps(diagnostics).casefold()
        assert "api_key" not in json.dumps(diagnostics).casefold()
    finally:
        database.dispose()


def test_glb_bbox_and_dimension_plan_prefer_uniform_scale(tmp_path: Path) -> None:
    path = tmp_path / "raw.glb"
    _glb_with_position_bounds(path, (-1.0, 0.0, -2.0), (1.0, 6.0, 2.0))
    bbox = extract_glb_bbox(path)
    assert bbox is not None
    assert bbox["size"] == {"width": 2.0, "depth": 4.0, "height": 6.0}
    plan = plan_dimension_normalization(bbox, {"width": 4, "depth": 8, "height": 12}, "m")
    assert plan["scale_factor"] == 2.0
    assert plan["dimension_status"] == "PASS"


def test_dimension_plan_rejects_obvious_non_uniform_deformation(tmp_path: Path) -> None:
    path = tmp_path / "raw.glb"
    _glb_with_position_bounds(path, (0.0, 0.0, 0.0), (2.0, 6.0, 4.0))
    with pytest.raises(ModelDimensionConflict):
        plan_dimension_normalization(extract_glb_bbox(path), {"width": 2, "depth": 8, "height": 6}, "m")


def test_blender_cli_final_dimension_conflict_uses_safe_domain_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = tmp_path / "raw.glb"
    output = tmp_path / "normalized.glb"
    _glb_with_position_bounds(raw, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))

    class _Result:
        returncode = 0

    def _fake_blender(*args, **kwargs):
        output.write_bytes(raw.read_bytes())
        return _Result()

    monkeypatch.setattr("workers.blender_adapter.subprocess.run", _fake_blender)
    monkeypatch.setattr("workers.blender_adapter.validate_glb", lambda path: (True, "ok"))
    bboxes = iter((
        {"size": {"width": 1.0, "depth": 1.0, "height": 1.0}},
        {"size": {"width": 1.5, "depth": 1.0, "height": 1.0}},
    ))
    monkeypatch.setattr("workers.blender_adapter.extract_glb_bbox", lambda path: next(bboxes))
    adapter = BlenderCLIAdapter("blender")
    with pytest.raises(ModelDimensionConflict):
        adapter.normalize_and_qa(
            raw,
            output,
            target_dimensions={"width": 1, "depth": 1, "height": 1},
            dimension_unit="m",
        )


def test_brain_dimension_schema_defaults_to_estimated() -> None:
    decision = BrainProductDecision(
        eligible=True,
        single_product=True,
        background_ok=True,
        image_to_3d_suitable=True,
    )
    assert decision.dimension_source == "AI_ESTIMATED"
