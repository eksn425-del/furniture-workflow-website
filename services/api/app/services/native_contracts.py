from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CountKind = Literal["EXACT", "ESTIMATED", "UNKNOWN"]

TaxonomyStrategy = Literal[
    "NAV_TREE", "JSON_LD_COLLECTION", "SITEMAP", "MAGENTO_GRAPHQL",
    "SHOPIFY_NAV", "GENERIC_CATEGORY_LINKS", "BROWSER_NAV",
]
ProductDiscoveryStrategy = Literal[
    "JSON_LD_PRODUCTS", "SHOPIFY_COLLECTION_JSON", "MAGENTO_GRAPHQL_PRODUCTS",
    "GENERIC_PRODUCT_CARDS", "SITEMAP_PRODUCTS", "BROWSER_PRODUCT_CARDS",
]
PaginationStrategy = Literal[
    "REL_NEXT", "NEXT_LINK", "PAGE_PARAM", "MAGENTO_CURRENT_PAGE",
    "SHOPIFY_PAGE", "LOAD_MORE", "BOUNDED_SCROLL", "NO_VERIFIED_CONTINUATION",
]
PDPStrategy = Literal["JSON_LD_PRODUCT", "VISIBLE_PDP_TEXT", "STRUCTURED_PUBLIC_API", "BROWSER_PDP"]
ImageStrategy = Literal["JSON_LD_IMAGE", "OG_IMAGE", "PRODUCT_GALLERY", "STRUCTURED_MEDIA_API", "BROWSER_GALLERY"]
DimensionStrategy = Literal[
    "STRUCTURED_DIMENSIONS", "PDP_DIMENSION_TEXT", "SPECIFICATION_PANEL",
    "PUBLIC_API_DIMENSIONS", "BROWSER_DIMENSION_PANEL",
]


class TaxonomyCategoryContract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 大脑返回的类目可能不携带 category_id；缺失时按 path 生成确定性 id。
    category_id: str = Field(default="", max_length=128)
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

    @model_validator(mode="after")
    def ensure_category_id(self) -> "TaxonomyCategoryContract":
        if not self.category_id:
            self.category_id = f"cat_{hashlib.sha256(self.path.encode('utf-8')).hexdigest()[:16]}"
        return self


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
    # Dimensions supplied by the Brain are estimates unless the Website has
    # already attached official structured/page evidence.  Keeping this field
    # explicit prevents a reviewer-entered estimate from being mislabeled as
    # an official catalog value.
    dimension_source: Literal["OFFICIAL_STRUCTURED", "OFFICIAL_PAGE", "AI_ESTIMATED", "UNKNOWN"] = "AI_ESTIMATED"
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
    site_profile: dict[str, object] | None = None
    agent_trace: dict[str, object] = Field(default_factory=dict)
    profile_version: str = "native-unverified"
    captured_at: datetime


class AgentToolCall(BaseModel):
    """One executed tool call recorded in the agent-loop receipt."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, object] = Field(default_factory=dict)
    result_status: str = "OK"
    error_code: str | None = None
    result_summary: dict[str, object] | None = None


class BrainAgentReceipt(BaseModel):
    """Receipt of a Brain agent-loop run (persisted with taxonomy evidence)."""

    model_config = ConfigDict(extra="ignore")

    status: str = "AGENT_READY"
    stopped_reason: str = "FINISH"
    stop_code: str = ""
    turns: int = 0
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    provider_posts: int = 0
    agent_run_id: str = ""
    task: str = ""
    selected_strategies: dict[str, str] = Field(default_factory=dict)


class SiteProfileContract(BaseModel):
    """Safe, durable site intelligence profile (schema v1).

    This model is separate from the SQLAlchemy ``SiteProfile`` row: the row
    stores this JSON contract while this model validates the boundary.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["website-site-profile.v1"] = "website-site-profile.v1"
    site_key: str = Field(min_length=1, max_length=255)
    source_url: str = Field(default="", max_length=2000)
    source_type: str = Field(default="UNKNOWN", min_length=1, max_length=64)
    platform: str = Field(default="UNKNOWN", min_length=1, max_length=64)
    taxonomy_strategy: TaxonomyStrategy
    product_discovery_strategy: ProductDiscoveryStrategy
    pagination_strategy: PaginationStrategy
    pdp_strategy: PDPStrategy
    image_strategy: ImageStrategy
    dimension_strategy: DimensionStrategy
    public_url_patterns: list[str] = Field(default_factory=list, max_length=16)
    safe_public_hints: list[str] = Field(default_factory=list, max_length=32)
    strategy_parameters: dict[str, object] = Field(default_factory=dict)
    status: Literal["DRAFT", "VALIDATED", "STALE", "BLOCKED"] = "DRAFT"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    validated_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    profile_version: str = Field(min_length=1, max_length=128)


# Versioned alias used by the integration surface.
SiteProfileV1 = SiteProfileContract

