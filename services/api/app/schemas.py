from __future__ import annotations

from datetime import date, datetime

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationInfo, field_validator


AuthorizationMode = Literal["COST_CEILING", "EXACT_COUNT_AUTHORIZATION"]


def _validate_progressive_gates(value: list[int]) -> list[int]:
    normalized = [int(item) for item in value]
    if normalized != sorted(set(normalized)):
        raise ValueError("progressive_gates must be strictly increasing and unique")
    if any(item < 1 or item > 500 for item in normalized):
        raise ValueError("progressive_gates must be between 1 and 500")
    return normalized


def _validate_company_test_count(value: int) -> int:
    if value not in {1, 3, 5}:
        raise ValueError("company test count must be exactly 1, 3, or 5")
    return value


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "furniture-workflow-api"
    version: str = "0.16.0"


class CapabilitiesResponse(BaseModel):
    qwen_configured: bool
    vision_provider: Literal["simulation", "host_agent", "local_agent", "qwen", "minimax", "qunhe_oneapi"]
    vision_ready: bool
    vision_can_lock_catalog: bool
    vision_asset_replay_ready: bool
    size_from_picture_key_configured: bool
    size_from_picture_enabled: bool
    dimension_extractor_policy_version: str
    scrape_probe_ready: bool
    scrape_queue_ready: bool
    scrape_worker_ready: bool
    vision_worker_ready: bool
    scraper_rules_version: str
    cgtrader_registry_ready: bool
    cgtrader_registry_snapshot_date: str | None = None
    cgtrader_generated_product_records: int = 0
    cgtrader_unique_block_keys: int = 0
    modeling_enabled: bool
    blender_worker_enabled: bool
    modeling_worker_ready: bool
    blender_worker_ready: bool
    qa_worker_ready: bool
    mvp_mode: bool


class CompanyTestSiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    target_type: Literal["AUTO", "Furniture", "3D Model Marketplace"] = "AUTO"
    sample_count: int = Field(default=1, ge=1, le=5)
    live: bool = True

    @field_validator("sample_count")
    @classmethod
    def validate_count(cls, value: int) -> int:
        return _validate_company_test_count(value)


class CompanyTestDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    count: int = Field(default=3, ge=1, le=5)
    products_xml_path: str | None = Field(default=None, max_length=1000)

    @field_validator("count")
    @classmethod
    def validate_count(cls, value: int) -> int:
        return _validate_company_test_count(value)


class CompanyTestProductionPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=8, max_length=100)
    requested_count: int = Field(default=1, ge=1, le=5)
    provider: Literal["lux3d", "tripo", "hunyuan"] = "lux3d"

    @field_validator("requested_count")
    @classmethod
    def validate_count(cls, value: int) -> int:
        return _validate_company_test_count(value)


class CompanyTestStartRequest(CompanyTestProductionPlanRequest):
    confirm: bool = False


class CompanyTestHumanVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=8, max_length=100)
    action: Literal["completed_visible_verification", "preflight_again"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    source_url: AnyHttpUrl
    site_key: str | None = Field(default=None, min_length=1, max_length=255)
    brand_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_and_validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("site_key")
    @classmethod
    def normalize_site_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
            raise ValueError("site_key contains unsupported characters")
        return value

    @field_validator("brand_name")
    @classmethod
    def normalize_brand_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            return None
        if any(character in '\\/*?:"<>|' or ord(character) < 32 for character in value):
            raise ValueError("brand_name contains unsupported filename characters")
        return value


class WorkflowOrderCreate(BaseModel):
    """One bounded instruction that authorizes one automatic workflow run."""

    model_config = ConfigDict(extra="forbid")

    source_url: AnyHttpUrl
    instruction: str = Field(min_length=4, max_length=2000)
    name: str | None = Field(default=None, max_length=200)
    brand_name: str | None = Field(default=None, max_length=100)
    site_key: str | None = Field(default=None, max_length=255)
    product_count: int = Field(default=3, ge=1, le=500)
    max_pages: int = Field(default=20, ge=1, le=50)
    provider: Literal["lux3d", "tripo", "hunyuan"] = "lux3d"
    output_mode: Literal["catalog", "glb"] = "glb"
    use_agent_adapter: bool = False
    authorization_mode: AuthorizationMode = "COST_CEILING"
    progressive_gates: list[int] = Field(default_factory=list, max_length=4)
    category_quotas: dict[str, int] = Field(default_factory=dict)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("name", "brand_name")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("site_key")
    @classmethod
    def normalize_order_site_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in normalized):
            raise ValueError("site_key contains unsupported characters")
        return normalized

    @field_validator("brand_name")
    @classmethod
    def normalize_brand_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            return None
        if any(character in '\\/*?:"<>|' or ord(character) < 32 for character in value):
            raise ValueError("brand_name contains unsupported filename characters")
        return value

    @field_validator("progressive_gates")
    @classmethod
    def validate_order_progressive_gates(cls, value: list[int]) -> list[int]:
        return _validate_progressive_gates(value)


class StageResponse(BaseModel):
    key: str
    label: str
    status: str
    completed_tasks: int = 0
    total_tasks: int = 0
    active_task: str | None = None


class ProjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: str
    name: str
    source_url: AnyHttpUrl
    site_key: str | None
    brand_name: str
    status: str
    current_stage: str
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectSummary):
    stages: list[StageResponse]


class ProjectListResponse(BaseModel):
    items: list[ProjectDetail]
    total: int


class StageViewResponse(BaseModel):
    project_id: str
    stages: list[StageResponse]


class DimensionVerificationJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=16, max_length=256)
    execution_mode: Literal["configured", "simulation"] = "configured"
    simulation_fixture: Literal["dual_ai_agreement_v1", "dual_ai_disagreement_v1"] | None = None


class DimensionBatchJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_assets: int | None = Field(default=None, ge=1, le=500)


class ScrapeJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=3, ge=1, le=5000)
    max_pages: int = Field(default=20, ge=1, le=200)
    request_budget: int | None = Field(default=None, ge=10, le=20000)
    download_media: bool = True
    use_agent_adapter: bool = False
    allow_ai_fallback: bool = True
    max_age_years: int = Field(default=0, ge=0, le=50)
    as_of_date: date | None = None


class ModelGenerationJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["lux3d", "tripo", "hunyuan"]
    requested_count: int = Field(ge=1, le=500)
    catalog_lock_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    estimated_cost_minor: int | None = Field(default=None, ge=0, le=100_000_000)
    approved_cost_ceiling_minor: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: Literal["CNY", "USD"] = "CNY"
    approval_confirmed: bool
    idempotency_key: str = Field(min_length=16, max_length=256)
    authorization_mode: AuthorizationMode = "COST_CEILING"
    progressive_gates: list[int] = Field(default_factory=list, max_length=4)
    category_quotas: dict[str, int] = Field(default_factory=dict)

    @field_validator("progressive_gates")
    @classmethod
    def validate_job_progressive_gates(cls, value: list[int]) -> list[int]:
        return _validate_progressive_gates(value)


class BlenderExportJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_lock_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=16, max_length=256)


class EnterpriseInputResponse(BaseModel):
    schema_version: int = 1
    original_filename: str
    stored_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    immutable: bool = True
    uploaded_at: str


class QualityCheckJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enterprise_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    use_dino: bool = True
    idempotency_key: str = Field(min_length=16, max_length=256)


class WorkflowJobEventResponse(BaseModel):
    sequence: int
    event_type: str
    status: str
    message: str
    current: int
    total: int
    payload: dict | None = None
    created_at: datetime


class WorkflowJobEventListResponse(BaseModel):
    items: list[WorkflowJobEventResponse]
    total: int


class ScrapeRecordResponse(BaseModel):
    record_id: str
    product_id: str
    sku: str = ""
    name: str
    original_name: str
    product_url: str
    image_url: str = ""
    dimension_image_url: str = ""
    size: str = ""
    raw_size: str = ""
    dimension_decision: str = "missing"
    warnings: list[str] = Field(default_factory=list)


class NamingConflictResponse(BaseModel):
    record_id: str
    original_name: str
    governed_name: str
    base_governed_name: str = ""
    final_governed_name: str = ""
    discriminators: list[dict[str, str]] = Field(default_factory=list)
    decision_source: str = ""
    disambiguation_strategy_version: str = ""
    sku: str = ""
    product_url: str = ""
    reasons: list[str] = Field(default_factory=list)


class GovernedNameOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    governed_name: str = Field(min_length=3, max_length=120)


class ScrapeEvidenceResponse(BaseModel):
    available: bool
    status: str = "not_started"
    rules_version: str | None = None
    generated_at: str | None = None
    requested_limit: int = 0
    discovered_count: int = 0
    record_count: int = 0
    records_with_explicit_wdh: int = 0
    records_with_dimension_image: int = 0
    asset_count: int = 0
    page_snapshot_count: int = 0
    failure_count: int = 0
    catalog_lock_allowed: bool = False
    agent_adapter: dict = Field(default_factory=dict)
    global_registry: dict = Field(default_factory=dict)
    global_registry_skip_count: int = 0
    selection_policy: dict = Field(default_factory=dict)
    records: list[ScrapeRecordResponse] = Field(default_factory=list)
    naming_conflicts: list[NamingConflictResponse] = Field(default_factory=list)
    downloads: dict[str, str] = Field(default_factory=dict)


class ReadinessBlocker(BaseModel):
    code: str
    message: str


class ModelProviderReadiness(BaseModel):
    provider: Literal["lux3d", "tripo", "hunyuan"]
    configured: bool
    selected: bool


class ModelingReadinessResponse(BaseModel):
    workflow_version: str
    skills_bundle_version: str
    model_filename_rule: str
    provider: Literal["lux3d", "tripo", "hunyuan"]
    providers: list[ModelProviderReadiness]
    catalog_lock_present: bool
    catalog_lock_status: str
    catalog_lock_format: str
    catalog_lock_id: str | None = None
    locked_catalog_sha256: str | None = None
    catalog_content_hash: str | None = None
    record_count: int = 0
    safe_ready_count: int = 0
    generated_model_count: int = 0
    generation_complete: bool = False
    final_model_count: int = 0
    blender_complete: bool = False
    export_complete: bool = False
    progressive_gates: list[int]
    next_gate: int | None = None
    modeling_enabled: bool
    blender_ready: bool
    can_prepare_images: bool
    can_generate_models: bool
    can_run_blender: bool
    can_export: bool
    blockers: list[ReadinessBlocker]


class ProviderTaskItemResponse(BaseModel):
    filename: str
    record_id: str | None = None
    identity_key: str | None = None
    status: str
    checkpoint_state: str | None = None
    reason: str | None = None
    provider_task_id: str | None = None
    provider_progress: float | None = None
    model_filename: str | None = None
    updated_at: str | None = None


class ProviderTaskMonitorResponse(BaseModel):
    total: int = 0
    completed: int = 0
    active: int = 0
    waiting: int = 0
    failed: int = 0
    blocked: int = 0
    max_provider_slots: int = 5
    active_provider_tasks: int = 0
    unresolved_provider_tasks: int = 0
    capacity_wait: int = 0
    manual_reconciliation: int = 0
    candidate_pool_updated_at: str | None = None
    items: list[ProviderTaskItemResponse] = Field(default_factory=list)


class WorkflowJobResponse(BaseModel):
    job_id: str
    project_id: str
    task_key: str
    input_sha256: str
    idempotency_key: str
    execution_mode: str
    simulation: bool
    status: str
    attempt_count: int
    result: dict | None = None
    failure_class: str | None = None
    failure_message: str | None = None
    created_at: datetime
    updated_at: datetime


class GovernedNameOverrideResponse(BaseModel):
    record_id: str
    governed_name: str
    evidence_sha256: str
    dimension_job: WorkflowJobResponse


class WorkflowJobListResponse(BaseModel):
    items: list[WorkflowJobResponse]
    total: int


class WorkflowOrderResponse(BaseModel):
    schema_version: int = 2
    project_id: str
    instruction: str
    product_count: int
    max_pages: int
    provider: Literal["lux3d", "tripo", "hunyuan"]
    output_mode: Literal["catalog", "glb"]
    use_agent_adapter: bool
    authorization_mode: AuthorizationMode = "COST_CEILING"
    progressive_gates: list[int] = Field(default_factory=list)
    category_quotas: dict[str, int] = Field(default_factory=dict)
    engine_schema_version: str = "workflow-engine.v8.8.1"
    order_policy_hash: str | None = None
    candidate_pool_path: str | None = None
    candidate_pool_summary: dict = Field(default_factory=dict)
    scope_authorized: bool = True
    status: str
    current_step: str
    message: str
    created_at: str
    updated_at: str
    exceptions: list[str] = Field(default_factory=list)
    project: ProjectDetail
    active_job: WorkflowJobResponse | None = None


# Website 1.0 operator-facing control-plane contracts.  They describe the
# Website's orchestration surface; Skills execution receipts remain external.
class ControlJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: AnyHttpUrl
    title: str = Field(default="未命名家具采集任务", max_length=200)
    goal: str = Field(default="发现家具产品并生成可交付模型", max_length=2000)
    target_mode: Literal["EXACT_N", "UP_TO_N", "ALL"] = "EXACT_N"
    target_value: int | None = Field(default=1, ge=1, le=5000)
    scope: Literal["NEW_ONLY", "TOTAL_INCLUDING_EXISTING"] = "NEW_ONLY"
    category_allocation: Literal["PER_CATEGORY", "TOTAL_ACROSS_SELECTED"] = "TOTAL_ACROSS_SELECTED"
    allocation_strategy: Literal["SEQUENTIAL", "EVEN", "PROPORTIONAL", "CUSTOM"] = "SEQUENTIAL"
    spillover: Literal["ASK", "AUTO_IF_EXPLICIT", "STOP"] = "ASK"
    category_ids: list[str] = Field(default_factory=list, max_length=100)
    provider: Literal["OFF", "lux3d", "tripo", "hunyuan"] = "OFF"

    @field_validator("title", "goal")
    @classmethod
    def normalize_control_text(cls, value: str, info: ValidationInfo) -> str:
        text = " ".join(value.split())
        defaults: dict[str, str] = {
            "title": "未命名家具采集任务",
            "goal": "发现家具产品并生成可交付模型",
        }
        return text if text else defaults.get(info.field_name or "", "未命名家具采集任务")

    @field_validator("target_value")
    @classmethod
    def validate_target_value(cls, value: int | None, info) -> int | None:
        mode = info.data.get("target_mode")
        if mode in {"EXACT_N", "UP_TO_N"} and value is None:
            raise ValueError("target_value is required for Exact N and Up To N")
        if mode == "ALL" and value is not None:
            return None
        return value


class ControlJobTargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["ACCEPT_SHORTAGE", "ADD_CATEGORY", "MODIFY_TARGET", "STOP"]
    target_value: int | None = Field(default=None, ge=1, le=5000)
    category_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=1000)


class ControlJobApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    approved_cost_ceiling_minor: int = Field(default=0, ge=0, le=100_000_000)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class ControlJobEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    goal: str | None = Field(default=None, max_length=2000)
    target_value: int | None = Field(default=None, ge=1, le=5000)
    provider: Literal["OFF", "lux3d", "tripo", "hunyuan"] | None = Field(default=None)


class ControlJobStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume_browser_session: bool = True

class HumanReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["CONFIRM", "EDIT", "REJECT", "REQUEST_RESCAN", "ACCEPT", "STOP"]
    reason: str = Field(default="", max_length=1000)
    actor: str = Field(default="operator", min_length=1, max_length=128)
