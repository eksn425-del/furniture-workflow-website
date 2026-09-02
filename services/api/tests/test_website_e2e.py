from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.main import create_app
from app.models import ProductionJob
from workers.local_e2e import LOCAL_CATEGORY_ID, LOCAL_PRODUCT_COUNT, LOCAL_SITE_KEY, LOCAL_SITE_URL, LocalMockSiteAnalyzer


def _wait_for(client: TestClient, path: str, predicate, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}: {json.dumps(last, ensure_ascii=False)[:4000]}")


def test_add_site_to_delivery_runs_complete_local_workflow(monkeypatch, tmp_path: Path) -> None:
    """Exercise the public control-plane flow without network or paid APIs."""

    monkeypatch.setenv("FURNITURE_WORKFLOW_LOCAL_E2E", "1")
    monkeypatch.setenv("LUX3D_API_KEY", "local-e2e-placeholder")
    monkeypatch.setenv("LUX3D_BASE_URL", "http://local-e2e.invalid")
    settings = Settings(output_root=tmp_path / "output", web_base_url="http://127.0.0.1:3000")
    app = create_app(settings)
    mock_analyzer = LocalMockSiteAnalyzer()
    app.state.site_analyzer = mock_analyzer
    app.state.site_scan_runtime.analyzer = mock_analyzer

    with TestClient(app) as client:
        preflight = client.post("/api/v1/control/site/preflight", json={"url": LOCAL_SITE_URL, "live": True})
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["status"] == "READY"
        scan_response = client.post(
            f"/api/v1/control/sites/{LOCAL_SITE_KEY}/scan",
            json={"url": LOCAL_SITE_URL, "live": True},
        )
        assert scan_response.status_code == 200, scan_response.text
        scan_id = scan_response.json()["scan_id"]
        site_detail = _wait_for(
            client,
            f"/api/v1/control/sites/{LOCAL_SITE_KEY}",
            lambda value: bool(value.get("scans")) and value["scans"][0]["scan_id"] == scan_id and value["scans"][0]["status"] == "READY",
        )
        assert site_detail["taxonomy_available"] is True
        assert site_detail["reported_total"] == LOCAL_PRODUCT_COUNT
        assert site_detail["categories"][0]["count_kind"] == "EXACT"

        created = client.post("/api/v1/control/jobs", json={
            "source_url": LOCAL_SITE_URL,
            "title": "Local E2E Chairs",
            "goal": "Run the complete Website workflow",
            "target_mode": "EXACT_N",
            "target_value": LOCAL_PRODUCT_COUNT,
            "scope": "NEW_ONLY",
            "category_allocation": "TOTAL_ACROSS_SELECTED",
            "allocation_strategy": "SEQUENTIAL",
            "spillover": "STOP",
            "category_ids": [LOCAL_CATEGORY_ID],
            "provider": "lux3d",
        })
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["job_id"]

        selected = client.post(f"/api/v1/control/jobs/{job_id}/target", json={
            "action": "ADD_CATEGORY",
            "category_ids": [LOCAL_CATEGORY_ID],
            "reason": "Bind the current READY taxonomy snapshot",
        })
        assert selected.status_code == 200, selected.text

        session = app.state.database.session_factory()
        try:
            job = session.get(ProductionJob, job_id)
            assert job is not None
            policy = json.loads(job.policy_json or "{}")
            policy["test_profile"] = "LOCAL_E2E"
            policy["provider_concurrency"] = 5
            job.policy_json = json.dumps(policy, ensure_ascii=False)
            session.commit()
        finally:
            session.close()

        approved = client.post(f"/api/v1/control/jobs/{job_id}/approve", json={
            "confirm": True,
            "approved_cost_ceiling_minor": 2100,
            "actor": "local-e2e-test",
        })
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "PRODUCTION_READY"

        started = client.post(f"/api/v1/control/jobs/{job_id}/start", json={"resume_browser_session": True})
        assert started.status_code == 200, started.text
        assert started.json()["started"] is True

        finished = _wait_for(
            client,
            f"/api/v1/control/jobs/{job_id}",
            lambda value: value.get("job", {}).get("status") in {"COMPLETED", "FAILED", "BLOCKED", "HUMAN_REQUIRED", "TARGET_SHORTAGE"},
        )
        assert finished["job"]["status"] == "COMPLETED", json.dumps(finished, ensure_ascii=False, indent=2)
        assert finished["run"]["status"] == "SUCCEEDED"
        assert finished["job"]["counts"]["delivered_count"] == LOCAL_PRODUCT_COUNT
        assert len(finished["provider_tasks"]) == LOCAL_PRODUCT_COUNT
        assert finished["candidate_pool"]["state_counts"].get("COMPLETED") == LOCAL_PRODUCT_COUNT
        assert all(item["name_char_count"] <= 50 for item in finished["candidate_pool"]["items"])
        assert all(item["review_provider"] == "LOCAL_AGENT" for item in finished["candidate_pool"]["items"])
        assert all(item["blender_qa_status"] == "PASS" for item in finished["candidate_pool"]["items"])
        assert {artifact["artifact_type"] for artifact in finished["artifacts"]} == {"DELIVERY_BATCH_ZIP", "MANIFEST_JSON"}
        assert all(artifact["status"] == "DELIVERED" for artifact in finished["artifacts"])
        assert any(event["event_type"] == "DISCOVERY_COMPLETED" for event in finished["events"])
        assert any(event["event_type"] == "JOB_COMPLETED" for event in finished["events"])

        deliveries = client.get("/api/v1/control/deliveries")
        assert deliveries.status_code == 200, deliveries.text
        delivery = next(item for item in deliveries.json()["items"] if item["job_id"] == job_id)
        assert delivery["batch_count"] == 2
        assert delivery["model_count"] == LOCAL_PRODUCT_COUNT
        assert [batch["file_count"] for batch in delivery["batches"]] == [20, 1]

        for batch in delivery["batches"]:
            artifact_id = batch["artifact_id"]
            download = client.get(f"/api/v1/control/deliveries/artifacts/{artifact_id}/download")
            assert download.status_code == 200, download.text
            with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                names = archive.namelist()
                assert names
                assert all(name.endswith(".glb") for name in names)
                assert len(names) == batch["file_count"]

        manifest_artifact = next(item for item in finished["artifacts"] if item["artifact_type"] == "MANIFEST_JSON")
        manifest_download = client.get(f"/api/v1/control/deliveries/artifacts/{manifest_artifact['artifact_id']}/download")
        assert manifest_download.status_code == 200, manifest_download.text
        manifest = manifest_download.json()
        assert all(item["target_dimensions"] for item in manifest["items"])
        assert all(item["dimension_source"] == "OFFICIAL_STRUCTURED" for item in manifest["items"])
        assert all(item["blender_qa"]["status"] == "PASS" for item in manifest["items"])
