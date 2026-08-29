from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CountKind = Literal["EXACT", "ESTIMATED", "UNKNOWN"]


class TaxonomyCategoryContract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category_id: str = Field(min_length=1, max_length=128)
    native_name: str = Field(min_length=1, max_length=255)
    canonical_name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=2000)
    source_url: str = ""
    count_value: int | None = Field(default=None, ge=0)
    count_kind: CountKind = "UNKNOWN"
    evidence: list[dict[str, object]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # 两级类目模型：level=1 为站点大类目，level=2 为其子类目；parent_path 指向所属一级路径。
    level: int = Field(default=1, ge=1, le=2)
    parent_path: str | None = None

    @field_validator("native_name", "canonical_name", "path")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return " ".join(value.split())


class BrainTaxonomyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    categories: list[TaxonomyCategoryContract] = Field(default_factory=list, max_length=100)
    reasoning: str = ""


class BrainSourceDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: Literal[
        "DIRECT_BRAND", "MULTI_BRAND_RETAILER", "MULTI_CATEGORY_RETAILER", "MARKETPLACE",
        "SCOPED_CATEGORY", "SEARCH_RESULT", "UNKNOWN",
    ] = "UNKNOWN"
    brand_display_name: str = ""
    scope_kind: Literal["SITE", "CATEGORY", "SEARCH", "MARKETPLACE_SCOPE", "UNKNOWN"] = "UNKNOWN"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=12)


class BrainAccessDecision(BaseModel):
    """Provider-independent decision for L0/L1/L2 access evidence."""

    model_config = ConfigDict(extra="ignore")

    access_state: Literal[
        "ACCESSIBLE", "ESCALATE_L2", "TEMPORARY_FAILURE", "HUMAN_REQUIRED",
        "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "STOP",
    ]
    next_action: Literal[
        "CONTINUE", "ESCALATE_L2", "RETRY_SAME_SESSION", "WAIT_FOR_HUMAN",
        "REQUIRE_ACCESS_CHANGE", "STOP",
    ]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=12)
    summary: str = ""


class BrainProductDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    eligible: bool
    single_product: bool
    background_ok: bool
    image_to_3d_suitable: bool
    category_group: str = ""
    style: str = ""
    color: str = ""
    material: str = ""
    product_type: str = ""
    feature: str = ""
    width: float | None = Field(default=None, gt=0)
    depth: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    dimension_unit: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=16)
    source_image_vision_consistent: bool | None = None
    reviewed_media_sha256: str = ""


class TaxonomyReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = "website-taxonomy-receipt.v3"
    site_key: str
    source_url: str
    live: bool
    status: str
    verified: bool = False
    fixture_only: bool = False
    taxonomy_level: str = "L0"
    source_type: Literal[
        "DIRECT_BRAND", "MULTI_BRAND_RETAILER", "MULTI_CATEGORY_RETAILER", "MARKETPLACE",
        "SCOPED_CATEGORY", "SEARCH_RESULT", "UNKNOWN",
    ] = "UNKNOWN"
    source_scope: str = "UNKNOWN"
    categories: list[TaxonomyCategoryContract] = Field(default_factory=list)
    evidence: dict[str, object] = Field(default_factory=dict)
    blocker: dict[str, str] | None = None
    brain: dict[str, object] = Field(default_factory=dict)
    profile_version: str = "native-unverified"
    captured_at: datetime
