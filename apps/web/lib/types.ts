export type WorkflowStatus =
  | "pending"
  | "ready"
  | "running"
  | "needs_review"
  | "blocked"
  | "succeeded"
  | "failed"
  | "cancelled"
  | string;

export interface StageSummary {
  key: string;
  label: string;
  status: WorkflowStatus;
  completed_tasks: number;
  total_tasks: number;
  active_task?: string | null;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  source_url: string;
  site_key?: string | null;
  brand_name: string;
  status: WorkflowStatus;
  current_stage?: string | null;
  created_at: string;
  updated_at: string;
  stages: StageSummary[];
}

export type ProjectDetail = ProjectSummary;

export interface ProjectListResponse {
  items: ProjectSummary[];
  total: number;
}

export interface CreateProjectInput {
  name: string;
  source_url: string;
  site_key?: string;
  brand_name?: string;
}

export interface CreateWorkflowOrderInput {
  source_url: string;
  instruction: string;
  name?: string;
  brand_name?: string;
  site_key?: string;
  product_count: number;
  max_pages: number;
  provider: "lux3d" | "tripo" | "hunyuan";
  output_mode: "catalog" | "glb";
  use_agent_adapter: boolean;
  authorization_mode?: "COST_CEILING" | "EXACT_COUNT_AUTHORIZATION";
  progressive_gates?: number[];
  category_quotas?: Record<string, number>;
}

export interface WorkflowOrder {
  schema_version: number;
  project_id: string;
  instruction: string;
  product_count: number;
  max_pages: number;
  provider: "lux3d" | "tripo" | "hunyuan";
  output_mode: "catalog" | "glb";
  use_agent_adapter: boolean;
  scope_authorized: boolean;
  status: WorkflowStatus;
  current_step: string;
  message: string;
  created_at: string;
  updated_at: string;
  exceptions: string[];
  project: ProjectDetail;
  active_job?: WorkflowJob | null;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
}

export interface CompanyTestResponse {
  [key: string]: unknown;
}

export interface CapabilitiesResponse {
  qwen_configured: boolean;
  minimax_configured?: boolean;
  vision_provider: "simulation" | "host_agent" | "local_agent" | "qwen" | "minimax" | "qunhe_oneapi";
  vision_ready: boolean;
  vision_can_lock_catalog: boolean;
  vision_asset_replay_ready: boolean;
  size_from_picture_key_configured: boolean;
  size_from_picture_enabled: boolean;
  dimension_extractor_policy_version: string;
  scrape_probe_ready: boolean;
  scrape_queue_ready: boolean;
  scrape_worker_ready: boolean;
  vision_worker_ready: boolean;
  scraper_rules_version: string;
  cgtrader_registry_ready: boolean;
  cgtrader_registry_snapshot_date?: string | null;
  cgtrader_generated_product_records: number;
  cgtrader_unique_block_keys: number;
  modeling_enabled: boolean;
  blender_worker_enabled: boolean;
  modeling_worker_ready: boolean;
  blender_worker_ready: boolean;
  qa_worker_ready: boolean;
  mvp_mode: boolean;
}

export interface WorkflowJob {
  job_id: string;
  project_id: string;
  task_key: string;
  input_sha256: string;
  idempotency_key: string;
  execution_mode: string;
  simulation: boolean;
  status: WorkflowStatus;
  attempt_count: number;
  result?: Record<string, unknown> | null;
  failure_class?: string | null;
  failure_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkflowJobListResponse {
  items: WorkflowJob[];
  total: number;
}

export interface WorkflowJobEvent {
  sequence: number;
  event_type: string;
  status: WorkflowStatus;
  message: string;
  current: number;
  total: number;
  payload?: Record<string, unknown> | null;
  created_at: string;
}

export interface WorkflowJobEventListResponse {
  items: WorkflowJobEvent[];
  total: number;
}

export interface CreateScrapeJobInput {
  limit: number;
  max_pages: number;
  download_media: boolean;
  request_budget?: number;
  use_agent_adapter?: boolean;
  allow_ai_fallback?: boolean;
  max_age_years?: number;
  as_of_date?: string;
}

export interface ScrapeRecord {
  record_id: string;
  product_id: string;
  sku: string;
  name: string;
  original_name: string;
  product_url: string;
  image_url: string;
  dimension_image_url: string;
  size: string;
  raw_size: string;
  dimension_decision: string;
  warnings: string[];
}

export interface NamingConflict {
  record_id: string;
  original_name: string;
  governed_name: string;
  sku: string;
  product_url: string;
  reasons: string[];
}

export interface ScrapeEvidence {
  available: boolean;
  status: string;
  rules_version?: string | null;
  generated_at?: string | null;
  requested_limit: number;
  discovered_count: number;
  record_count: number;
  records_with_explicit_wdh: number;
  records_with_dimension_image: number;
  asset_count: number;
  page_snapshot_count: number;
  failure_count: number;
  catalog_lock_allowed: boolean;
  agent_adapter: Record<string, unknown>;
  global_registry: Record<string, unknown>;
  global_registry_skip_count: number;
  selection_policy: Record<string, unknown>;
  records: ScrapeRecord[];
  naming_conflicts: NamingConflict[];
  downloads: Record<string, string>;
}

export interface ReadinessBlocker {
  code: string;
  message: string;
}

export interface ModelProviderReadiness {
  provider: "lux3d" | "tripo" | "hunyuan";
  configured: boolean;
  selected: boolean;
}

export interface ModelingReadiness {
  workflow_version: string;
  skills_bundle_version: string;
  model_filename_rule: string;
  provider: "lux3d" | "tripo" | "hunyuan";
  providers: ModelProviderReadiness[];
  catalog_lock_present: boolean;
  catalog_lock_status: string;
  catalog_lock_format: string;
  catalog_lock_id?: string | null;
  locked_catalog_sha256?: string | null;
  record_count: number;
  safe_ready_count: number;
  generated_model_count: number;
  generation_complete: boolean;
  final_model_count: number;
  blender_complete: boolean;
  export_complete: boolean;
  progressive_gates: number[];
  next_gate?: number | null;
  modeling_enabled: boolean;
  blender_ready: boolean;
  can_prepare_images: boolean;
  can_generate_models: boolean;
  can_run_blender: boolean;
  can_export: boolean;
  blockers: ReadinessBlocker[];
}

export interface ProviderTaskItem {
  filename: string;
  record_id?: string | null;
  identity_key?: string | null;
  status: string;
  checkpoint_state?: string | null;
  reason?: string | null;
  provider_task_id?: string | null;
  provider_progress?: number | null;
  model_filename?: string | null;
  updated_at?: string | null;
}

export interface ProviderTaskMonitor {
  total: number;
  completed: number;
  active: number;
  waiting: number;
  failed: number;
  blocked: number;
  max_provider_slots: number;
  active_provider_tasks: number;
  unresolved_provider_tasks: number;
  capacity_wait: number;
  manual_reconciliation: number;
  candidate_pool_updated_at?: string | null;
  items: ProviderTaskItem[];
}

export interface ModelGenerationJobInput {
  provider: "lux3d" | "tripo" | "hunyuan";
  requested_count: number;
  catalog_lock_sha256: string;
  estimated_cost_minor: number;
  approved_cost_ceiling_minor: number;
  currency: "CNY" | "USD";
  approval_confirmed: boolean;
  idempotency_key: string;
}

export interface EnterpriseInput {
  schema_version: number;
  original_filename: string;
  stored_filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  immutable: boolean;
  uploaded_at: string;
}
