"""Offline qualification of acceptance evidence; never a production gate."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def locked_record(record: dict[str, Any]) -> bool:
    """Require the durable lock payload, its digest and candidate binding."""
    lineage = record.get("lineage") or {}
    payload = lineage.get("model_input")
    if record.get("state") != "MODEL_INPUT_LOCKED" or not isinstance(payload, dict):
        return False
    if not record.get("candidate_id") or payload.get("candidate_id") != record["candidate_id"]:
        return False
    if not all(payload.get(key) for key in ("image_sha256", "catalog_lock_hash", "dimensions")):
        return False
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return lineage.get("model_input_hash") == digest


def qualify(row: dict[str, Any]) -> str:
    """Failures dominate; status labels alone never establish external blocking."""
    layers = [row.get(f"layer_{key}") for key in "bcd"]
    objects = list(_objects({"evidence": row.get("evidence"), "layers": layers}))
    statuses = {str(obj.get(key) or "").upper() for obj in objects for key in ("status", "raw_status", "error_code", "error_type", "error_classification", "error", "code", "reason_code")}
    if "PASS_BLOCKED" in statuses or row.get("qualification") == "PASS_BLOCKED" or row.get("layer_a") == "PASS_BLOCKED":
        return "FAIL_CODE"
    if "CODE_DEFECT" in statuses:
        return "FAIL_CODE"
    if statuses & {"ENVIRONMENT_BLOCKED", "BROWSER_RUNTIME_MISSING", "BROWSER_RUNTIME_NOT_INSTALLED", "BROWSERRUNTIMEMISSING", "NETWORK_ERROR", "BRAIN_NOT_CONFIGURED", "VISION_PROVIDER_NOT_CONFIGURED", "SSLERROR", "SSLEOFERROR", "SSLCERTVERIFICATIONERROR", "CONNECTIONERROR", "CONNECTTIMEOUT", "GAIERROR"}:
        return "FAIL_ENVIRONMENT"
    if row.get("layer_a") == "ENVIRONMENT_BLOCKED":
        return "FAIL_ENVIRONMENT"
    external = {"ROBOTS_DENIED", "CAPTCHA", "WAF_BLOCKED", "LOGIN_REQUIRED", "HTTP_403", "HTTP_429", "ACCESS_DENIED", "ACCESS_CONTROL_DETECTED"}
    for obj in objects:
        codes = {str(obj.get(key) or "").upper() for key in ("code", "reason_code", "error_code")}
        if codes & external and any(obj.get(key) for key in ("message", "url", "evidence", "evidence_path")):
            return "BLOCKED_EXTERNAL"
    if statuses & {"UNCLASSIFIED_ERROR", "SCAN_TIMEOUT", "TIMEOUTERROR", "FAILED"} or row.get("layer_a") == "FAILED":
        return "FAIL_CODE"
    d = row.get("layer_d") or {}
    records = [item for item in d.get("records", []) if isinstance(item, dict)]
    locked = {item["candidate_id"] for item in records if locked_record(item)}
    target = row.get("target_count")
    if isinstance(target, int) and not isinstance(target, bool) and target > 0 and len(locked) >= target:
        return "PASS_READY"
    taxonomy = row.get("layer_b") or {}
    if (taxonomy.get("raw_status") == "READY" and taxonomy.get("verified") is True
            and taxonomy.get("live") is True and not taxonomy.get("fixture_only")
            and isinstance(taxonomy.get("categories"), list) and taxonomy["categories"]
            and all(isinstance(item, dict) and item.get("source_url") for item in taxonomy["categories"])):
        return "CATALOG_READY"
    if any(item.get("state") == "CATALOG_READY" and (item.get("lineage") or {}).get("catalog_lock_hash") for item in records):
        return "CATALOG_READY"
    return "PARTIAL_EVIDENCE"
