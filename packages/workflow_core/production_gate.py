"""Website-local deterministic pre-provider Production Gate.

Keep this implementation local to the Website package: the native Website
runtime must not import Skills.  Its contract and behavior are kept in parity
with ``Skills/skills/shared/workflow_core/production_gate.py`` by tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .source_policy import MAX_FINAL_NAME_CHARS, SourcePolicy, resolve_source_policy

READY_FOR_MODELING = "READY_FOR_MODELING"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
L2_BROWSER_REQUIRED = "L2_BROWSER_REQUIRED"
MEDIA_IDENTITY_MISMATCH = "MEDIA_IDENTITY_MISMATCH"
SCOPE_VISUAL_CONFLICT = "SCOPE_VISUAL_CONFLICT"

_PASS = {"PASS", "PASSED", "VERIFIED", "ACCEPTED", "MATCH", "CLEAR", "READY", "UNIQUE", "NEW", "CAPTURED", "CLAIMED"}
_NAME_PROHIBITED = re.compile(
    r"(?:\bcgtrader\b|\b3d\s*model\b|\bfree\b|\b(?:sku|model)\s*[-#:]*\s*[a-z0-9-]+\b|"
    r"\d+(?:\.\d+)?\s*(?:inches?|in|cm|mm|m|w|d|h)\b|\d+(?:\.\d+)?\s*[x×]\s*\d+)", re.IGNORECASE,
)


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _status(value: object) -> str:
    return _text(value).upper()


def _truth(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    token = _status(value)
    if token in _PASS:
        return True
    if token in {"FAIL", "FAILED", "REJECTED", "MISMATCH", "CONFLICT", "DUPLICATE", "BLOCKED", "UNKNOWN"}:
        return False
    return None


def _first(facts: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in facts and facts[key] not in (None, ""):
            return facts[key]
    return None


def _dimensions_ok(facts: Mapping[str, Any]) -> bool | None:
    raw = _first(facts, "dimensions", "governed_dimensions", "source_dimensions")
    if not isinstance(raw, Mapping):
        raw = facts
    # 至少提供一个有效轴即为有尺寸证据：AI 预估只给高度时，宽度/深度由
    # Blender 等比缩放按模型比例补出，因此高度存在即可通过。
    seen = False
    for axis in ("width", "depth", "height"):
        value = raw.get(axis)
        if value in (None, ""):
            continue
        seen = True
        try:
            if float(value) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True if seen else None


@dataclass(frozen=True)
class ProductionGateDecision:
    status: str
    reasons: tuple[str, ...] = ()
    checks: dict[str, str] = field(default_factory=dict)
    source_host: str = "*"

    @property
    def ready(self) -> bool:
        return self.status == READY_FOR_MODELING

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "ready": self.ready, "reasons": list(self.reasons), "checks": dict(self.checks), "source_host": self.source_host}


def evaluate_production_gate(facts: Mapping[str, Any], *, source: object = "", source_policy: SourcePolicy | None = None) -> ProductionGateDecision:
    policy = source_policy or resolve_source_policy(source)
    checks: dict[str, str] = {}
    reasons: list[str] = []
    identity = _text(_first(facts, "identity", "record_identity", "identity_key", "asset_identity"))
    checks["identity"] = "PASS" if identity else "MISSING"
    if not identity: reasons.append("identity_missing")
    dedup = _status(_first(facts, "dedup_status", "dedup", "duplicate_status"))
    if dedup in {"DUPLICATE", "SKIP_DUPLICATE", "CONFLICT"}:
        checks["dedup"] = "FAIL"; reasons.append("duplicate_identity")
    elif dedup in _PASS: checks["dedup"] = "PASS"
    else: checks["dedup"] = "MISSING"; reasons.append("dedup_unverified")
    claim = _status(_first(facts, "claim_status", "claim", "global_claim_status"))
    if claim in {"CAPTURED", "CLAIMED", "RECLAIMED"}: checks["claim"] = "PASS"
    elif claim in {"STALE", "RELEASED"}: checks["claim"] = "FAIL"; reasons.append("claim_not_active")
    else: checks["claim"] = "MISSING"; reasons.append("claim_missing")
    binding = _status(_first(facts, "media_binding_status", "binding_status", "media_binding"))
    try: binding_confidence = float(_first(facts, "media_binding_confidence", "binding_confidence") or 0)
    except (TypeError, ValueError): binding_confidence = 0.0
    if binding == "MISMATCH": checks["media_binding"] = "FAIL"; reasons.append("media_identity_mismatch")
    elif binding == "EXACT" or (binding == "COMPATIBLE" and binding_confidence >= 0.85): checks["media_binding"] = "PASS"
    elif binding == "UNKNOWN": checks["media_binding"] = "L2"; reasons.append("media_binding_unknown")
    else: checks["media_binding"] = "MISSING"; reasons.append("media_binding_missing")
    decodable = _truth(_first(facts, "image_decodable", "media_decodable", "decodable_image"))
    if decodable is True: checks["decodable_image"] = "PASS"
    elif decodable is False: checks["decodable_image"] = "FAIL"; reasons.append("image_not_decodable")
    else: checks["decodable_image"] = "MISSING"; reasons.append("image_decodability_unverified")
    visual_state = _status(_first(facts, "visual_review_status", "visual_review", "vision_status"))
    try: confidence = float(_first(facts, "visual_confidence", "confidence") or 0)
    except (TypeError, ValueError): confidence = 0.0
    if visual_state in {"FAIL", "FAILED", "REJECTED", "BLOCKED"}: checks["visual_review"] = "FAIL"; reasons.append("visual_review_rejected")
    elif visual_state in _PASS:
        if "confidence" in facts or "visual_confidence" in facts:
            if confidence < 0.65: checks["visual_review"] = "FAIL"; reasons.append("visual_confidence_below_0_65")
            elif confidence < 0.85: checks["visual_review"] = "REVIEW"; reasons.append("visual_confidence_requires_second_review")
            else: checks["visual_review"] = "PASS"
        else: checks["visual_review"] = "PASS"
    else: checks["visual_review"] = "MISSING"; reasons.append("visual_review_missing")
    consistency = _truth(_first(facts, "source_image_vision_consistent", "source_image_vision_consistency", "source_image_vision"))
    if consistency is True: checks["source_image_vision_consistency"] = "PASS"
    elif consistency is False: checks["source_image_vision_consistency"] = "FAIL"; reasons.append("source_image_vision_conflict")
    else: checks["source_image_vision_consistency"] = "MISSING"; reasons.append("source_image_vision_unverified")
    scope = _truth(_first(facts, "scope_status", "scope", "scope_visual_consistency"))
    if scope is True: checks["scope"] = "PASS"
    elif scope is False: checks["scope"] = "FAIL"; reasons.append("scope_visual_conflict")
    else: checks["scope"] = "MISSING"; reasons.append("scope_unverified")
    name_raw = _first(facts, "final_name", "product_name", "name")
    name = _text(name_raw)
    checks["final_name"] = "PASS" if name else "MISSING"
    if not name: reasons.append("final_name_missing")
    elif len(name) > MAX_FINAL_NAME_CHARS: checks["final_name"] = "FAIL"; reasons.append("final_name_exceeds_50_characters")
    elif name != str(name_raw).strip() or "  " in name or _NAME_PROHIBITED.search(name): checks["final_name"] = "FAIL"; reasons.append("final_name_safety_invalid")
    naming_status = _status(_first(facts, "name_status", "naming_status"))
    if naming_status in {"REVIEW_REQUIRED", "BLOCKED", "FAILED", "CONFLICT"}:
        checks["final_name"] = "FAIL"; reasons.append("naming_review_required")
    dimension_state = _dimensions_ok(facts)
    if dimension_state is True: checks["dimensions"] = "PASS"
    elif dimension_state is False: checks["dimensions"] = "FAIL"; reasons.append("dimensions_invalid")
    else: checks["dimensions"] = "MISSING"; reasons.append("dimensions_missing")
    idempotency = _text(_first(facts, "provider_idempotency_key", "idempotency_key", "model_input_hash"))
    checks["provider_idempotency"] = "PASS" if idempotency else "MISSING"
    if not idempotency: reasons.append("provider_idempotency_missing")
    unique_reasons = tuple(dict.fromkeys(reasons))
    if "media_identity_mismatch" in unique_reasons: status = MEDIA_IDENTITY_MISMATCH
    elif "scope_visual_conflict" in unique_reasons: status = SCOPE_VISUAL_CONFLICT
    elif "media_binding_unknown" in unique_reasons: status = L2_BROWSER_REQUIRED
    elif not unique_reasons: status = READY_FOR_MODELING
    else: status = REVIEW_REQUIRED
    return ProductionGateDecision(status, unique_reasons, checks, policy.source_host)


class ProductionGate:
    def __init__(self, *, source: object = "", source_policy: SourcePolicy | None = None) -> None:
        self.source_policy = source_policy or resolve_source_policy(source)

    def evaluate(self, facts: Mapping[str, Any]) -> ProductionGateDecision:
        return evaluate_production_gate(facts, source_policy=self.source_policy)


__all__ = ["L2_BROWSER_REQUIRED", "MEDIA_IDENTITY_MISMATCH", "ProductionGate", "ProductionGateDecision", "READY_FOR_MODELING", "REVIEW_REQUIRED", "SCOPE_VISUAL_CONFLICT", "evaluate_production_gate"]
