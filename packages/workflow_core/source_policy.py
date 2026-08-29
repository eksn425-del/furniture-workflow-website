"""Website-local loader for the neutral Source Policy contract.

This file intentionally does not import Skills.  The JSON contract is copied
into both packages and parity is tested in CI/release checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Final, Mapping


SOURCE_POLICY_PATH: Final[Path] = Path(__file__).with_name("source_policy.v1.json")
SOURCE_POLICY_VERSION: Final[str] = "source-policy.v1"
MAX_FINAL_NAME_CHARS: Final[int] = 50


def validate_source_policy(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != SOURCE_POLICY_VERSION:
        raise ValueError("source_policy_schema_version_invalid")
    if payload.get("max_final_name_chars") != MAX_FINAL_NAME_CHARS:
        raise ValueError("source_policy_name_limit_invalid")
    profiles = payload.get("source_profiles")
    if not isinstance(profiles, Mapping) or not {"cgtrader.com", "roomandboard.com", "*"}.issubset(profiles):
        raise ValueError("source_policy_required_profile_missing")
    for host, profile in profiles.items():
        if not isinstance(profile, Mapping):
            raise ValueError(f"source_policy_profile_invalid:{host}")
        for key in ("source_kind", "title_authority", "type_authority", "category_authority", "vision_policy", "identity_policy", "media_policy", "brand_prefix_policy", "naming_contract_version"):
            if not str(profile.get(key) or "").strip():
                raise ValueError(f"source_policy_field_missing:{host}:{key}")


try:
    SOURCE_POLICY: Final[dict[str, Any]] = json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise RuntimeError(f"source policy unavailable: {SOURCE_POLICY_PATH}") from error
validate_source_policy(SOURCE_POLICY)


@dataclass(frozen=True)
class SourcePolicy:
    source_host: str
    source_kind: str
    title_authority: str
    type_authority: str
    category_authority: str
    vision_policy: str
    identity_policy: str
    media_policy: str
    brand_display_name: str
    brand_prefix_policy: str
    naming_contract_version: str

    @property
    def max_final_name_chars(self) -> int:
        return MAX_FINAL_NAME_CHARS

    @property
    def is_visual_first(self) -> bool:
        return self.type_authority == "VISION_PRIMARY"

    @property
    def is_brand_direct(self) -> bool:
        return self.source_kind == "DIRECT_BRAND"

    def naming_kwargs(self) -> dict[str, str]:
        return {
            "brand": self.brand_display_name,
            "brand_prefix_policy": self.brand_prefix_policy,
            "contract_version": self.naming_contract_version,
        }


def _host(value: object) -> str:
    raw = str(value or "").strip().casefold()
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def resolve_source_policy(source: object) -> SourcePolicy:
    host = _host(source)
    profiles = SOURCE_POLICY["source_profiles"]
    profile = profiles.get(host) or profiles.get("*")
    assert isinstance(profile, Mapping)
    return SourcePolicy(
        source_host=host or "*",
        source_kind=str(profile.get("source_kind") or "UNKNOWN"),
        title_authority=str(profile.get("title_authority") or "LOW"),
        type_authority=str(profile.get("type_authority") or "VISION_REQUIRED"),
        category_authority=str(profile.get("category_authority") or "DISCOVERY_ONLY"),
        vision_policy=str(profile.get("vision_policy") or "VISUAL_GOVERNED"),
        identity_policy=str(profile.get("identity_policy") or "SOURCE_BOUND"),
        media_policy=str(profile.get("media_policy") or "SOURCE_BOUND"),
        brand_display_name=str(profile.get("brand_display_name") or ""),
        brand_prefix_policy=str(profile.get("brand_prefix_policy") or "NONE"),
        naming_contract_version=str(profile.get("naming_contract_version") or "naming-contract.v1"),
    )


__all__ = [
    "MAX_FINAL_NAME_CHARS",
    "SOURCE_POLICY",
    "SOURCE_POLICY_PATH",
    "SOURCE_POLICY_VERSION",
    "SourcePolicy",
    "resolve_source_policy",
    "validate_source_policy",
]
