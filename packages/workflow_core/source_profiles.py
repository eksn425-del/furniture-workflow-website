"""Source-neutral acquisition and public-name policy profiles.

The profile is configuration, not a site-specific branch in the naming core.
Skills and Website can therefore use the same naming contract for a brand-direct
retailer and a marketplace without changing the deterministic composer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .naming import NAMING_CONTRACT_V1, NAMING_CONTRACT_V2, NAMING_CONTRACT_V2_BRAND_DIRECT, NAMING_CONTRACT_V3_MARKETPLACE


SOURCE_KINDS = frozenset({"BRAND_DIRECT", "MARKETPLACE", "MODEL_MARKETPLACE", "MODEL_REPOSITORY", "GENERIC_RETAILER", "UNKNOWN"})
BRAND_POLICIES = frozenset({"REQUIRED", "OPTIONAL", "NONE"})


@dataclass(frozen=True)
class SourceProfile:
    source_site: str
    source_kind: str = "UNKNOWN"
    brand_display_name: str = ""
    brand_prefix_policy: str = "NONE"
    naming_contract_version: str = NAMING_CONTRACT_V1

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"source_kind_invalid:{self.source_kind}")
        if self.brand_prefix_policy not in BRAND_POLICIES:
            raise ValueError(f"brand_prefix_policy_invalid:{self.brand_prefix_policy}")
        if self.brand_prefix_policy == "REQUIRED" and not self.brand_display_name.strip():
            raise ValueError("brand_display_name_required")
        if self.naming_contract_version not in {NAMING_CONTRACT_V1, NAMING_CONTRACT_V2, NAMING_CONTRACT_V2_BRAND_DIRECT, NAMING_CONTRACT_V3_MARKETPLACE}:
            raise ValueError(f"naming_contract_version_invalid:{self.naming_contract_version}")

    @property
    def is_brand_direct(self) -> bool:
        return self.source_kind == "BRAND_DIRECT"

    def naming_kwargs(self) -> dict[str, str]:
        return {
            "brand": self.brand_display_name,
            "brand_prefix_policy": self.brand_prefix_policy,
            "contract_version": self.naming_contract_version,
        }


ROOM_AND_BOARD_PROFILE = SourceProfile(
    source_site="roomandboard.com",
    source_kind="BRAND_DIRECT",
    brand_display_name="Room & Board",
    brand_prefix_policy="REQUIRED",
    naming_contract_version=NAMING_CONTRACT_V2_BRAND_DIRECT,
)

CGTRADER_PROFILE = SourceProfile(
    source_site="cgtrader.com",
    source_kind="MARKETPLACE",
    brand_display_name="",
    brand_prefix_policy="NONE",
    naming_contract_version=NAMING_CONTRACT_V3_MARKETPLACE,
)


__all__ = [
    "BRAND_POLICIES",
    "CGTRADER_PROFILE",
    "ROOM_AND_BOARD_PROFILE",
    "SOURCE_KINDS",
    "SourceProfile",
]
