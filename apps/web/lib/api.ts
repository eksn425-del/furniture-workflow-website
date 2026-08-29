import type {
  CapabilitiesResponse,
  CreateScrapeJobInput,
  CreateProjectInput,
  HealthResponse,
  ModelProviderReadiness,
  ProviderTaskMonitor,
  NamingConflict,
  ModelingReadiness,
  ModelGenerationJobInput,
  ProjectDetail,
  ProjectListResponse,
  ProjectSummary,
  StageSummary,
  ScrapeEvidence,
  ScrapeRecord,
  WorkflowJob,
  WorkflowJobEvent,
  WorkflowJobEventListResponse,
  WorkflowJobListResponse,
  EnterpriseInput,
  CreateWorkflowOrderInput,
  WorkflowOrder,
  CompanyTestResponse,
} from "@/lib/types";

const configuredBase = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();

export const API_BASE_URL = (configuredBase || "http://127.0.0.1:8000").replace(
  /\/+$/,
  "",
);

export const API_ROOT = API_BASE_URL.endsWith("/api/v1")
  ? API_BASE_URL
  : `${API_BASE_URL}/api/v1`;

const REQUEST_TIMEOUT_MS = 12_000;

export class ApiError extends Error {
  status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new ApiError(`API 响应缺少有效字段：${field}`);
  }
  return value;
}

function optionalString(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return value;
  return typeof value === "string" ? value : undefined;
}

function nonNegativeNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new ApiError(`API 响应缺少有效非负数：${field}`);
  }
  return value;
}

function normalizeStage(value: unknown): StageSummary {
  if (!isRecord(value)) {
    throw new ApiError("API 返回了无法识别的阶段数据");
  }

  return {
    key: stringValue(value.key, "stages[].key"),
    label: stringValue(value.label, "stages[].label"),
    status: stringValue(value.status, "stages[].status"),
    completed_tasks: nonNegativeNumber(
      value.completed_tasks,
      "stages[].completed_tasks",
    ),
    total_tasks: nonNegativeNumber(value.total_tasks, "stages[].total_tasks"),
    active_task: optionalString(value.active_task),
  };
}

function normalizeProject(value: unknown): ProjectSummary {
  if (!isRecord(value)) {
    throw new ApiError("API 返回了无法识别的项目数据");
  }

  return {
    project_id: stringValue(value.project_id, "project_id"),
    name: stringValue(value.name, "name"),
    source_url: stringValue(value.source_url, "source_url"),
    site_key: optionalString(value.site_key),
    brand_name: stringValue(value.brand_name, "brand_name"),
    status: stringValue(value.status, "status"),
    current_stage: optionalString(value.current_stage),
    created_at: stringValue(value.created_at, "created_at"),
    updated_at: stringValue(value.updated_at, "updated_at"),
    stages: Array.isArray(value.stages)
      ? value.stages.map(normalizeStage)
      : [],
  };
}

function normalizeJob(value: unknown): WorkflowJob {
  if (!isRecord(value)) throw new ApiError("API 返回了无法识别的任务数据");
  return {
    job_id: stringValue(value.job_id, "job_id"),
    project_id: stringValue(value.project_id, "project_id"),
    task_key: stringValue(value.task_key, "task_key"),
    input_sha256: stringValue(value.input_sha256, "input_sha256"),
    idempotency_key: stringValue(value.idempotency_key, "idempotency_key"),
    execution_mode: stringValue(value.execution_mode, "execution_mode"),
    simulation: value.simulation === true,
    status: stringValue(value.status, "status"),
    attempt_count: nonNegativeNumber(value.attempt_count, "attempt_count"),
    result: isRecord(value.result) ? value.result : null,
    failure_class: optionalString(value.failure_class),
    failure_message: optionalString(value.failure_message),
    created_at: stringValue(value.created_at, "created_at"),
    updated_at: stringValue(value.updated_at, "updated_at"),
  };
}

function normalizeWorkflowOrder(value: unknown): WorkflowOrder {
  if (!isRecord(value)) throw new ApiError("API 返回了无法识别的自动任务");
  const provider: WorkflowOrder["provider"] =
    value.provider === "tripo" || value.provider === "hunyuan"
      ? value.provider
      : "lux3d";
  return {
    schema_version: nonNegativeNumber(value.schema_version, "schema_version"),
    project_id: stringValue(value.project_id, "project_id"),
    instruction: stringValue(value.instruction, "instruction"),
    product_count: nonNegativeNumber(value.product_count, "product_count"),
    max_pages: nonNegativeNumber(value.max_pages, "max_pages"),
    provider,
    output_mode: value.output_mode === "catalog" ? "catalog" : "glb",
    use_agent_adapter: value.use_agent_adapter === true,
    scope_authorized: value.scope_authorized === true,
    status: stringValue(value.status, "status"),
    current_step: stringValue(value.current_step, "current_step"),
    message: stringValue(value.message, "message"),
    created_at: stringValue(value.created_at, "created_at"),
    updated_at: stringValue(value.updated_at, "updated_at"),
    exceptions: Array.isArray(value.exceptions)
      ? value.exceptions.filter((item): item is string => typeof item === "string")
      : [],
    project: normalizeProject(value.project),
    active_job: isRecord(value.active_job) ? normalizeJob(value.active_job) : null,
  };
}

function detailFromPayload(payload: unknown, fallback: string): string {
  if (!isRecord(payload)) return fallback;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    const first = payload.detail[0];
    if (isRecord(first) && typeof first.msg === "string") return first.msg;
  }
  return fallback;
}

async function request(
  path: string,
  init?: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<unknown> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    timeoutMs,
  );

  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
      signal: controller.signal,
    });

    const payload: unknown = await response.json().catch(() => undefined);

    if (!response.ok) {
      throw new ApiError(
        detailFromPayload(payload, `API 请求失败（HTTP ${response.status}）`),
        response.status,
      );
    }

    return payload;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("API 请求超时。请确认 FastAPI 服务与网络连接正常。");
    }
    throw new ApiError(
      `无法连接到 FastAPI（${API_ROOT}）。请确认服务已启动、地址正确，并允许当前站点跨域访问。`,
    );
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function getHealth(): Promise<HealthResponse> {
  const payload = await request("/health");
  if (!isRecord(payload)) throw new ApiError("API 健康检查响应无效");
  return {
    status: stringValue(payload.status, "status"),
    service: stringValue(payload.service, "service"),
    version: stringValue(payload.version, "version"),
  };
}

function companyTestRecord(value: unknown): CompanyTestResponse {
  if (!isRecord(value)) throw new ApiError("诊断接口返回了无法识别的响应");
  return value;
}

export async function getCompanyDoctor(): Promise<CompanyTestResponse> {
  return companyTestRecord(await request("/system/doctor", undefined, 60_000));
}

export async function runCompanyQualification(input: {
  url: string;
  sample_count: 1 | 3 | 5;
  live: boolean;
}): Promise<CompanyTestResponse> {
  return companyTestRecord(await request("/site/qualify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }, 300_000));
}

export async function runCompanyDryRun(input: {
  url: string;
  count: 1 | 3 | 5;
}): Promise<CompanyTestResponse> {
  return companyTestRecord(await request("/jobs/dry-run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }, 300_000));
}

export async function createWorkflowOrder(
  input: CreateWorkflowOrderInput,
): Promise<WorkflowOrder> {
  return normalizeWorkflowOrder(await request("/workflow-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }));
}

export async function getWorkflowOrder(projectId: string): Promise<WorkflowOrder> {
  return normalizeWorkflowOrder(await request(
    `/projects/${encodeURIComponent(projectId)}/autopilot`,
  ));
}

export async function advanceWorkflowOrder(projectId: string): Promise<WorkflowOrder> {
  return normalizeWorkflowOrder(await request(
    `/projects/${encodeURIComponent(projectId)}/autopilot/advance`,
    { method: "POST" },
  ));
}

export async function getCapabilities(): Promise<CapabilitiesResponse> {
  const payload = await request("/capabilities");
  if (!isRecord(payload)) throw new ApiError("API 能力响应无效");
  return {
    qwen_configured: payload.qwen_configured === true,
    minimax_configured: payload.minimax_configured === true,
    vision_provider:
      payload.vision_provider === "host_agent" ||
      payload.vision_provider === "local_agent" ||
      payload.vision_provider === "qwen" ||
      payload.vision_provider === "minimax"
        ? payload.vision_provider
        : "simulation",
    vision_ready: payload.vision_ready === true,
    vision_can_lock_catalog: payload.vision_can_lock_catalog === true,
    vision_asset_replay_ready: payload.vision_asset_replay_ready === true,
    size_from_picture_key_configured:
      payload.size_from_picture_key_configured === true,
    size_from_picture_enabled: payload.size_from_picture_enabled === true,
    dimension_extractor_policy_version: stringValue(
      payload.dimension_extractor_policy_version,
      "dimension_extractor_policy_version",
    ),
    scrape_probe_ready: payload.scrape_probe_ready === true,
    scrape_queue_ready: payload.scrape_queue_ready === true,
    scrape_worker_ready: payload.scrape_worker_ready === true,
    vision_worker_ready: payload.vision_worker_ready === true,
    scraper_rules_version: stringValue(
      payload.scraper_rules_version,
      "scraper_rules_version",
    ),
    cgtrader_registry_ready: payload.cgtrader_registry_ready === true,
    cgtrader_registry_snapshot_date: optionalString(payload.cgtrader_registry_snapshot_date),
    cgtrader_generated_product_records: nonNegativeNumber(
      payload.cgtrader_generated_product_records,
      "cgtrader_generated_product_records",
    ),
    cgtrader_unique_block_keys: nonNegativeNumber(
      payload.cgtrader_unique_block_keys,
      "cgtrader_unique_block_keys",
    ),
    modeling_enabled: payload.modeling_enabled === true,
    blender_worker_enabled: payload.blender_worker_enabled === true,
    modeling_worker_ready: payload.modeling_worker_ready === true,
    blender_worker_ready: payload.blender_worker_ready === true,
    qa_worker_ready: payload.qa_worker_ready === true,
    mvp_mode: payload.mvp_mode === true,
  };
}

export async function getModelingReadiness(
  projectId: string,
  provider: "lux3d" | "tripo" | "hunyuan",
): Promise<ModelingReadiness> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/modeling/readiness?provider=${encodeURIComponent(provider)}`,
  );
  if (!isRecord(payload)) throw new ApiError("模型阶段状态响应无效");
  const providers = Array.isArray(payload.providers)
    ? payload.providers.filter(isRecord).map((item): ModelProviderReadiness => {
        const normalizedProvider: ModelProviderReadiness["provider"] =
          item.provider === "tripo" || item.provider === "hunyuan"
            ? item.provider
            : "lux3d";
        return {
          provider: normalizedProvider,
          configured: item.configured === true,
          selected: item.selected === true,
        };
      })
    : [];
  const blockers = Array.isArray(payload.blockers)
    ? payload.blockers.filter(isRecord).map((item) => ({
        code: stringValue(item.code, "blockers[].code"),
        message: stringValue(item.message, "blockers[].message"),
      }))
    : [];
  return {
    workflow_version: stringValue(payload.workflow_version, "workflow_version"),
    skills_bundle_version: stringValue(payload.skills_bundle_version, "skills_bundle_version"),
    model_filename_rule: stringValue(payload.model_filename_rule, "model_filename_rule"),
    provider:
      payload.provider === "tripo" || payload.provider === "hunyuan"
        ? payload.provider
        : "lux3d",
    providers,
    catalog_lock_present: payload.catalog_lock_present === true,
    catalog_lock_status: stringValue(payload.catalog_lock_status, "catalog_lock_status"),
    catalog_lock_format: stringValue(payload.catalog_lock_format, "catalog_lock_format"),
    catalog_lock_id: optionalString(payload.catalog_lock_id),
    locked_catalog_sha256: optionalString(payload.locked_catalog_sha256),
    record_count: nonNegativeNumber(payload.record_count, "record_count"),
    safe_ready_count: nonNegativeNumber(payload.safe_ready_count, "safe_ready_count"),
    generated_model_count: nonNegativeNumber(
      payload.generated_model_count,
      "generated_model_count",
    ),
    generation_complete: payload.generation_complete === true,
    final_model_count: nonNegativeNumber(payload.final_model_count, "final_model_count"),
    blender_complete: payload.blender_complete === true,
    export_complete: payload.export_complete === true,
    progressive_gates: Array.isArray(payload.progressive_gates)
      ? payload.progressive_gates.filter(
          (item): item is number => typeof item === "number" && item > 0,
        )
      : [],
    next_gate: typeof payload.next_gate === "number" ? payload.next_gate : null,
    modeling_enabled: payload.modeling_enabled === true,
    blender_ready: payload.blender_ready === true,
    can_prepare_images: payload.can_prepare_images === true,
    can_generate_models: payload.can_generate_models === true,
    can_run_blender: payload.can_run_blender === true,
    can_export: payload.can_export === true,
    blockers,
  };
}

export async function getModelTaskMonitor(projectId: string): Promise<ProviderTaskMonitor> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/modeling/tasks`,
  );
  if (!isRecord(payload)) throw new ApiError("模型任务监控响应无效");
  const items = Array.isArray(payload.items)
    ? payload.items.filter(isRecord).map((item) => ({
        filename: stringValue(item.filename, "items[].filename"),
        record_id: optionalString(item.record_id),
        identity_key: optionalString(item.identity_key),
        status: stringValue(item.status, "items[].status"),
        checkpoint_state: optionalString(item.checkpoint_state),
        reason: optionalString(item.reason),
        provider_task_id: optionalString(item.provider_task_id),
        provider_progress:
          typeof item.provider_progress === "number" ? item.provider_progress : null,
        model_filename: optionalString(item.model_filename),
        updated_at: optionalString(item.updated_at),
      }))
    : [];
  return {
    total: nonNegativeNumber(payload.total, "total"),
    completed: nonNegativeNumber(payload.completed, "completed"),
    active: nonNegativeNumber(payload.active, "active"),
    waiting: nonNegativeNumber(payload.waiting, "waiting"),
    failed: nonNegativeNumber(payload.failed, "failed"),
    blocked: nonNegativeNumber(payload.blocked, "blocked"),
    max_provider_slots: nonNegativeNumber(payload.max_provider_slots, "max_provider_slots"),
    active_provider_tasks: nonNegativeNumber(payload.active_provider_tasks ?? 0, "active_provider_tasks"),
    unresolved_provider_tasks: nonNegativeNumber(payload.unresolved_provider_tasks ?? 0, "unresolved_provider_tasks"),
    capacity_wait: nonNegativeNumber(payload.capacity_wait ?? 0, "capacity_wait"),
    manual_reconciliation: nonNegativeNumber(payload.manual_reconciliation ?? 0, "manual_reconciliation"),
    candidate_pool_updated_at: optionalString(payload.candidate_pool_updated_at),
    items,
  };
}

export async function createModelGenerationJob(
  projectId: string,
  input: ModelGenerationJobInput,
): Promise<WorkflowJob> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/jobs/model-generation`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
  return normalizeJob(payload);
}

export async function createBlenderExportJob(
  projectId: string,
  catalogLockSha256: string,
): Promise<WorkflowJob> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/jobs/blender-export`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        catalog_lock_sha256: catalogLockSha256,
        idempotency_key: `blender:${catalogLockSha256.slice(0, 32)}`,
      }),
    },
  );
  return normalizeJob(payload);
}

function normalizeEnterpriseInput(value: unknown): EnterpriseInput {
  if (!isRecord(value)) throw new ApiError("企业商品库响应无效");
  return {
    schema_version: nonNegativeNumber(value.schema_version, "schema_version"),
    original_filename: stringValue(value.original_filename, "original_filename"),
    stored_filename: stringValue(value.stored_filename, "stored_filename"),
    media_type: stringValue(value.media_type, "media_type"),
    size_bytes: nonNegativeNumber(value.size_bytes, "size_bytes"),
    sha256: stringValue(value.sha256, "sha256"),
    immutable: value.immutable === true,
    uploaded_at: stringValue(value.uploaded_at, "uploaded_at"),
  };
}

export async function getEnterpriseInput(projectId: string): Promise<EnterpriseInput | null> {
  try {
    return normalizeEnterpriseInput(await request(
      `/projects/${encodeURIComponent(projectId)}/enterprise-input`,
    ));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export async function uploadEnterpriseInput(
  projectId: string,
  file: File,
): Promise<EnterpriseInput> {
  return normalizeEnterpriseInput(await request(
    `/projects/${encodeURIComponent(projectId)}/enterprise-input`,
    {
      method: "PUT",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": file.name,
      },
      body: file,
    },
  ));
}

export async function createQualityCheckJob(
  projectId: string,
  enterpriseInputSha256: string,
  useDino: boolean,
): Promise<WorkflowJob> {
  return normalizeJob(await request(
    `/projects/${encodeURIComponent(projectId)}/jobs/quality-check`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enterprise_input_sha256: enterpriseInputSha256,
        use_dino: useDino,
        idempotency_key: `qa:${enterpriseInputSha256.slice(0, 32)}:${useDino ? "dino" : "clip"}`,
      }),
    },
  ));
}

export async function getProjects(): Promise<ProjectListResponse> {
  const payload = await request("/projects");
  if (!isRecord(payload) || !Array.isArray(payload.items)) {
    throw new ApiError("API 项目列表响应无效");
  }

  return {
    items: payload.items.map(normalizeProject),
    total: nonNegativeNumber(payload.total, "total"),
  };
}

export async function createProject(
  input: CreateProjectInput,
): Promise<ProjectDetail> {
  const payload = await request("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return normalizeProject(payload);
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  const payload = await request(`/projects/${encodeURIComponent(projectId)}`);
  return normalizeProject(payload);
}

export async function getProjectJobs(
  projectId: string,
): Promise<WorkflowJobListResponse> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/jobs`,
  );
  if (!isRecord(payload) || !Array.isArray(payload.items)) {
    throw new ApiError("API 任务列表响应无效");
  }
  return {
    items: payload.items.map(normalizeJob),
    total: nonNegativeNumber(payload.total, "total"),
  };
}

export async function createScrapeJob(
  projectId: string,
  input: CreateScrapeJobInput,
): Promise<WorkflowJob> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/jobs/scrape`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
  return normalizeJob(payload);
}

export async function createDimensionBatchJob(
  projectId: string,
  maxAssets?: number,
): Promise<WorkflowJob> {
  return normalizeJob(await request(
    `/projects/${encodeURIComponent(projectId)}/jobs/dimension-batch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_assets: maxAssets ?? null }),
    },
  ));
}

export async function cancelWorkflowJob(
  projectId: string,
  jobId: string,
): Promise<WorkflowJob> {
  return normalizeJob(
    await request(
      `/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    ),
  );
}

export async function retryWorkflowJob(
  projectId: string,
  jobId: string,
): Promise<WorkflowJob> {
  return normalizeJob(
    await request(
      `/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}/retry`,
      { method: "POST" },
    ),
  );
}

function normalizeJobEvent(value: unknown): WorkflowJobEvent {
  if (!isRecord(value)) throw new ApiError("API 返回了无法识别的任务事件");
  return {
    sequence: nonNegativeNumber(value.sequence, "events[].sequence"),
    event_type: stringValue(value.event_type, "events[].event_type"),
    status: stringValue(value.status, "events[].status"),
    message: stringValue(value.message, "events[].message"),
    current: nonNegativeNumber(value.current, "events[].current"),
    total: nonNegativeNumber(value.total, "events[].total"),
    payload: isRecord(value.payload) ? value.payload : null,
    created_at: stringValue(value.created_at, "events[].created_at"),
  };
}

export async function getWorkflowJobEvents(
  projectId: string,
  jobId: string,
): Promise<WorkflowJobEventListResponse> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}/events`,
  );
  if (!isRecord(payload) || !Array.isArray(payload.items)) {
    throw new ApiError("API 任务事件响应无效");
  }
  return {
    items: payload.items.map(normalizeJobEvent),
    total: nonNegativeNumber(payload.total, "total"),
  };
}

function normalizeScrapeRecord(value: unknown): ScrapeRecord {
  if (!isRecord(value)) throw new ApiError("API 抓取记录响应无效");
  return {
    record_id: stringValue(value.record_id, "records[].record_id"),
    product_id: stringValue(value.product_id, "records[].product_id"),
    sku: stringValue(value.sku, "records[].sku"),
    name: stringValue(value.name, "records[].name"),
    original_name: stringValue(value.original_name, "records[].original_name"),
    product_url: stringValue(value.product_url, "records[].product_url"),
    image_url: stringValue(value.image_url, "records[].image_url"),
    dimension_image_url: stringValue(
      value.dimension_image_url,
      "records[].dimension_image_url",
    ),
    size: stringValue(value.size, "records[].size"),
    raw_size: stringValue(value.raw_size, "records[].raw_size"),
    dimension_decision: stringValue(
      value.dimension_decision,
      "records[].dimension_decision",
    ),
    warnings: Array.isArray(value.warnings)
      ? value.warnings.filter((item): item is string => typeof item === "string")
      : [],
  };
}

function normalizeNamingConflict(value: unknown): NamingConflict {
  if (!isRecord(value)) throw new ApiError("API 返回了无法识别的名称冲突");
  return {
    record_id: stringValue(value.record_id, "naming_conflicts[].record_id"),
    original_name: stringValue(value.original_name, "naming_conflicts[].original_name"),
    governed_name: stringValue(value.governed_name, "naming_conflicts[].governed_name"),
    sku: stringValue(value.sku, "naming_conflicts[].sku"),
    product_url: stringValue(value.product_url, "naming_conflicts[].product_url"),
    reasons: Array.isArray(value.reasons)
      ? value.reasons.filter((item): item is string => typeof item === "string")
      : [],
  };
}

export async function getScrapeEvidence(
  projectId: string,
): Promise<ScrapeEvidence> {
  const payload = await request(`/projects/${encodeURIComponent(projectId)}/scrape`);
  if (!isRecord(payload)) throw new ApiError("API 抓取证据响应无效");
  const downloads: Record<string, string> = {};
  if (isRecord(payload.downloads)) {
    for (const [key, value] of Object.entries(payload.downloads)) {
      if (typeof value === "string") downloads[key] = value;
    }
  }
  return {
    available: payload.available === true,
    status: stringValue(payload.status, "status"),
    rules_version: optionalString(payload.rules_version),
    generated_at: optionalString(payload.generated_at),
    requested_limit: nonNegativeNumber(payload.requested_limit, "requested_limit"),
    discovered_count: nonNegativeNumber(payload.discovered_count, "discovered_count"),
    record_count: nonNegativeNumber(payload.record_count, "record_count"),
    records_with_explicit_wdh: nonNegativeNumber(
      payload.records_with_explicit_wdh,
      "records_with_explicit_wdh",
    ),
    records_with_dimension_image: nonNegativeNumber(
      payload.records_with_dimension_image,
      "records_with_dimension_image",
    ),
    asset_count: nonNegativeNumber(payload.asset_count, "asset_count"),
    page_snapshot_count: nonNegativeNumber(
      payload.page_snapshot_count,
      "page_snapshot_count",
    ),
    failure_count: nonNegativeNumber(payload.failure_count, "failure_count"),
    catalog_lock_allowed: payload.catalog_lock_allowed === true,
    agent_adapter: isRecord(payload.agent_adapter) ? payload.agent_adapter : {},
    global_registry: isRecord(payload.global_registry) ? payload.global_registry : {},
    global_registry_skip_count: nonNegativeNumber(
      payload.global_registry_skip_count,
      "global_registry_skip_count",
    ),
    selection_policy: isRecord(payload.selection_policy) ? payload.selection_policy : {},
    records: Array.isArray(payload.records)
      ? payload.records.map(normalizeScrapeRecord)
      : [],
    naming_conflicts: Array.isArray(payload.naming_conflicts)
      ? payload.naming_conflicts.map(normalizeNamingConflict)
      : [],
    downloads,
  };
}

export async function setGovernedName(
  projectId: string,
  recordId: string,
  governedName: string,
): Promise<WorkflowJob> {
  const payload = await request(
    `/projects/${encodeURIComponent(projectId)}/scrape/records/${encodeURIComponent(recordId)}/governed-name`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ governed_name: governedName }),
    },
  );
  if (!isRecord(payload) || !isRecord(payload.dimension_job)) {
    throw new ApiError("名称已保存，但系统没有返回新的尺寸任务");
  }
  return normalizeJob(payload.dimension_job);
}

export function apiDownloadUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith("/api/v1") && API_BASE_URL.endsWith("/api/v1")) {
    return `${API_BASE_URL.slice(0, -7)}${path}`;
  }
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

export function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "发生了未知错误";
}
