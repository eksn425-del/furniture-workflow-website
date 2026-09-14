"""Offline regression coverage for the campaign's evidence-only classifier."""

import hashlib
import json

import pytest

from scripts.acceptance_qualification import qualify


def record(candidate="one", state="MODEL_INPUT_LOCKED"):
    payload = {"candidate_id": candidate, "image_sha256": "image", "catalog_lock_hash": "catalog", "dimensions": {"width": 1}}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"candidate_id": candidate, "state": state, "lineage": {"model_input": payload, "model_input_hash": digest, "catalog_lock_hash": "catalog"}}


def test_only_distinct_hash_bound_model_locks_meet_target():
    row = {"target_count": 3, "layer_d": {"records": [record(str(i)) for i in range(3)]}}
    assert qualify(row) == "PASS_READY"
    row["layer_d"]["records"] = [record()] * 3
    assert qualify(row) == "PARTIAL_EVIDENCE"
    row["layer_d"]["records"] = [record(str(i), "COMPLETED") for i in range(3)]
    assert qualify(row) == "PARTIAL_EVIDENCE"


def test_catalog_is_separate_from_model_readiness():
    assert qualify({"target_count": 1, "layer_d": {"records": [record(state="CATALOG_READY")]}}) == "CATALOG_READY"


def test_verified_completed_live_taxonomy_is_catalog_ready():
    taxonomy = {"raw_status": "READY", "verified": True, "live": True, "categories": [{"source_url": "https://example.test/chairs"}]}
    row = {"target_count": 1, "layer_b": taxonomy, "layer_d": {"status": "NOT_RUN"}}
    assert qualify(row) == "CATALOG_READY"
    taxonomy["categories"] = []
    assert qualify(row) == "PARTIAL_EVIDENCE"
    taxonomy["categories"] = [{"source_url": "https://example.test/chairs"}]
    taxonomy["fixture_only"] = True
    assert qualify(row) == "PARTIAL_EVIDENCE"


@pytest.mark.parametrize("error", ["SSLError", "BrowserRuntimeMissing", "ConnectionError"])
def test_explicit_environment_evidence_overrides_generic_failed(error):
    row = {"layer_a": "FAILED", "evidence": {"status": "FAILED", "error_type": error}}
    assert qualify(row) == "FAIL_ENVIRONMENT"
    row["layer_c"] = {"status": "CODE_DEFECT"}
    assert qualify(row) == "FAIL_CODE"


def test_tampered_or_missing_lock_is_not_ready():
    item = record()
    item["lineage"]["model_input"]["dimensions"] = {"width": 2}
    assert qualify({"target_count": 1, "layer_d": {"records": [item]}}) == "PARTIAL_EVIDENCE"
    assert qualify({"target_count": 1, "ready_count": 3, "layer_d": {"status": "PASS"}}) == "PARTIAL_EVIDENCE"


@pytest.mark.parametrize("status", ["ACCESS_BLOCKED", "BROWSER_REQUIRED", "HUMAN_REQUIRED"])
def test_status_without_external_evidence_does_not_prove_block(status):
    assert qualify({"layer_b": {"status": status}}) == "PARTIAL_EVIDENCE"


def test_explicit_external_evidence_and_failure_precedence():
    row = {"evidence": {"blocker": {"code": "ROBOTS_DENIED", "message": "Disallowed by robots policy"}}}
    assert qualify(row) == "BLOCKED_EXTERNAL"
    row["layer_b"] = {"status": "CODE_DEFECT"}
    assert qualify(row) == "FAIL_CODE"


@pytest.mark.parametrize("status,expected", [("BROWSER_RUNTIME_MISSING", "FAIL_ENVIRONMENT"), ("UNCLASSIFIED_ERROR", "FAIL_CODE"), ("PASS_BLOCKED", "FAIL_CODE"), ("NOT_RUN", "PARTIAL_EVIDENCE")])
def test_failure_and_unrun_classification(status, expected):
    row = {"found_count": "UNKNOWN", "layer_c": {"status": status}}
    assert qualify(row) == expected
    assert row["found_count"] == "UNKNOWN"
