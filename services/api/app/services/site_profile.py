"""SiteProfile v1 helpers owned by the Website control plane.

Profiles contain only reviewed public-site strategy data and evidence.  They
are deliberately safe to persist in the existing ``site_profiles`` JSON
column; secrets and browser/session state are rejected before persistence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.services.native_contracts import SiteProfileContract
from app.services.strategy_catalog import StrategyPlan, default_strategy_plan, validate_strategy_plan


SECRET_KEYS = frozenset({
    "cookie", "cookies", "credential", "credentials", "password", "token", "auth_token",
    "authorization", "api_key", "apikey", "session_secret", "browser_profile", "user_data_dir",
})


def _validate_public_source_url(value: str) -> None:
    if not value:
        return
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("site_profile_source_url_must_be_public_http_url")


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in SECRET_KEYS or any(part in normalized for part in ("cookie", "password", "token", "secret")):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_secret_key(child) for child in value)
    return False


def profile_version_for(plan: Mapping[str, Any], evidence: object) -> str:
    payload = json.dumps({"plan": dict(plan), "evidence": evidence}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"site-profile-v1-{hashlib.sha256(payload).hexdigest()[:12]}"


def build_site_profile(
    *,
    site_key: str,
    source_url: str,
    source_type: str,
    platform: str,
    plan: StrategyPlan | Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    status: str = "DRAFT",
    confidence: float = 0.0,
    created_at: datetime | None = None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = plan.as_dict() if isinstance(plan, StrategyPlan) else dict(plan or default_strategy_plan(platform).as_dict())
    safe_evidence = dict(evidence or {})
    now = created_at or datetime.now(UTC)
    previous_created = previous.get("created_at") if isinstance(previous, Mapping) else None
    parsed_source = urlsplit(source_url) if str(source_url).strip() else None
    payload = {
        "schema_version": "website-site-profile.v1",
        "site_key": str(site_key).strip(),
        "source_url": str(source_url).strip(),
        "source_type": str(source_type or "UNKNOWN").strip().upper(),
        "platform": str(platform or "UNKNOWN").strip().upper(),
        **selected,
        # A blocked/offline receipt may legitimately have no source URL yet;
        # never persist a syntactically misleading ``:///*`` pattern.
        "public_url_patterns": [f"{parsed_source.scheme}://{parsed_source.netloc}/*"] if parsed_source and parsed_source.hostname else [],
        "safe_public_hints": ["same_site", "robots_checked", "read_only", "bounded_requests"],
        "strategy_parameters": {},
        "status": str(status or "DRAFT").strip().upper(),
        "confidence": max(0.0, min(float(confidence or 0.0), 1.0)),
        "evidence": safe_evidence,
        "created_at": previous_created or now,
        "validated_at": now if str(status).upper() == "VALIDATED" else None,
        "last_success_at": now if str(status).upper() == "VALIDATED" else None,
        "last_failure_at": None,
        "profile_version": profile_version_for(selected, safe_evidence),
    }
    return validate_site_profile(payload, allow_draft=True).model_dump(mode="json")


def validate_site_profile(payload: Mapping[str, Any], *, allow_draft: bool = True) -> SiteProfileContract:
    if _contains_secret_key(payload):
        raise ValueError("site_profile_secret_or_session_field_forbidden")
    valid, errors = validate_strategy_plan(payload)
    if not valid:
        raise ValueError("site_profile_strategy_invalid:" + ",".join(errors))
    profile = SiteProfileContract.model_validate(payload)
    _validate_public_source_url(profile.source_url)
    if not allow_draft and profile.status != "VALIDATED":
        raise ValueError("site_profile_not_validated")
    return profile


def profile_is_reusable(payload: Mapping[str, Any] | None, *, site_key: str, platform: str | None = None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    try:
        profile = validate_site_profile(payload, allow_draft=False)
    except (TypeError, ValueError):
        return False
    if profile.site_key != site_key or profile.status != "VALIDATED":
        return False
    return not platform or profile.platform in {"UNKNOWN", str(platform).upper()}


def profile_drift_reasons(
    payload: Mapping[str, Any] | None,
    *,
    site_key: str,
    platform: str | None = None,
    plan: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return deterministic reasons a stored profile must be relearned."""

    if not isinstance(payload, Mapping):
        return ["PROFILE_MISSING"]
    try:
        profile = validate_site_profile(payload, allow_draft=True)
    except (TypeError, ValueError):
        return ["PROFILE_INVALID"]
    reasons: list[str] = []
    if profile.site_key != site_key:
        reasons.append("SITE_KEY_CHANGED")
    if platform and profile.platform not in {"UNKNOWN", str(platform).upper()}:
        reasons.append("PLATFORM_CHANGED")
    if plan:
        for field in ("taxonomy_strategy", "product_discovery_strategy", "pagination_strategy", "pdp_strategy", "image_strategy", "dimension_strategy"):
            old = str(getattr(profile, field, ""))
            new = str(plan.get(field) or "")
            if new and old != new:
                reasons.append(f"{field.upper()}_CHANGED")
    if profile.status == "STALE":
        reasons.append("PROFILE_MARKED_STALE")
    return reasons


__all__ = [
    "SECRET_KEYS", "build_site_profile", "profile_drift_reasons", "profile_is_reusable", "profile_version_for",
    "validate_site_profile",
]
