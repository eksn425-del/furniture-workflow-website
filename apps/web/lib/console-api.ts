import { API_ROOT } from "@/lib/api";

export type ControlStatus =
  | "DRAFT"
  | "TAXONOMY_READY"
  | "POLICY_READY"
  | "RUNNING"
  | "WAITING_REVIEW"
  | "HUMAN_REQUIRED"
  | "TARGET_SHORTAGE"
  | "PROVIDER_RUNNING"
  | "PRODUCTION_BLOCKED"
  | "COMPLETED"
  | "STOPPED"
  | "APPROVAL_RECORDED"
  | "REVIEW_RESOLVED"
  | "BRAIN_NOT_CONFIGURED"
  | string;

export interface ControlJob {
  job_id: string;
  title: string;
  source_url: string;
  site_key: string;
  site_name: string;
  goal: string;
  status: ControlStatus;
  current_stage: string;
  target_mode: "EXACT_N" | "UP_TO_N" | "ALL";
  target_value: number | null;
  scope: "NEW_ONLY" | "TOTAL_INCLUDING_EXISTING";
  category_allocation: "PER_CATEGORY" | "TOTAL_ACROSS_SELECTED";
  allocation_strategy: "SEQUENTIAL" | "EVEN" | "PROPORTIONAL" | "CUSTOM";
  spillover: "ASK" | "AUTO_IF_EXPLICIT" | "STOP";
  requested_count: number;
  counts: {
    reported_count: number;
    discovered_count: number;
    unique_count: number;
    eligible_count: number;
    ready_count: number;
    delivered_count: number;
    shortage_count: number;
  };
  provider: string;
  provider_calls: number;
  provider_safety: string;
  last_reason: string | null;
  policy: Record<string, unknown>;
  run?: ProductionRun | null;
  created_at: string;
  updated_at: string;
}

export interface ControlCategory {
  category_id: string;
  site_key: string;
  native_name: string;
  canonical_name: string;
  path: string;
  source_url: string;
  count_value: number | null;
  count_kind: "EXACT" | "ESTIMATED" | "UNKNOWN" | string;
  confidence: number;
  evidence: Array<Record<string, unknown>>;
  verified_at: string | null;
  reported_count: number;
  discovered_count: number;
  eligible_count: number;
  selected: boolean;
  level?: number;
  parent_path?: string | null;
  parent_category_id?: string | null;
  scope_kind?: string;
  last_scanned_at: string | null;
}

export interface ControlArtifact {
  artifact_id: string;
  job_id: string;
  run_id: string | null;
  artifact_type: string;
  status: string;
  relative_path: string;
  sha256: string | null;
  size_bytes: number;
  item_count: number;
  manifest_schema: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ControlReview {
  review_id: string;
  job_id?: string | null;
  reason_code: string;
  title: string;
  detail: string;
  severity: string;
  status: string;
  created_at: string;
}

export interface ControlOverview {
  schema_version: string;
  generated_at: string;
  demo_data: boolean;
  action_cards: {
    running: number;
    waiting_review: number;
    human_required: number;
    failed: number;
    provider_running: number;
    delivered_today: number;
  };
  metrics: {
    today_output: number;
    success_rate: number;
    average_duration_hours: number;
    cost_minor: number;
    workers_online: number;
  };
  jobs: ControlJob[];
  reviews: ControlReview[];
  provider_queue: { running: number; waiting: number; capacity: number; unknown: number };
}

export interface SystemStatus {
  schema_version: string;
  skills: { runtime_mode: string; root_configured: boolean; bundled: boolean; doctor: Record<string, unknown> };
  website_brain: { status: string; configured: boolean; model: string | null; namespace: string; provider_posts: number; model_mode?: string; review_provider?: string };
  provider: { status: string; provider_calls: number; safety_gate: string };
  database: { engine: string; status: string };
  object_storage: { status: string; note: string };
  workers: Record<string, string>;
  runtime_agent_dependency: string;
  feature_flags: Record<string, unknown>;
}

const CONTROL_ROOT = `${API_ROOT}/control`;

// 把任意错误负载安全地转成可读文本，绝不返回 [object Object]。
// 把常见的 FastAPI/Pydantic 英文校验错误翻译成中文。
export function zhApiError(message: string): string {
  const rules: Array<[RegExp, string]> = [
    [/String should have at least 1 character/i, "内容不能为空，请填写后再提交"],
    [/Input should be a valid URL|URL scheme not permitted|String should match pattern/i, "网址格式不正确，请输入类似 https://example.com 的完整网址"],
    [/Field required/i, "缺少必填字段"],
    [/Input should be a valid integer/i, "请输入整数"],
    [/Input should be '([^']+)'/i, "取值不合法（应为 $1）"],
    [/Input should be greater than or equal to (\d+)/i, "数值不能小于 $1"],
    [/Input should be less than or equal to (\d+)/i, "数值不能大于 $1"],
    [/greater than or equal to/i, "数值超出允许范围"],
    [/less than or equal to/i, "数值超出允许范围"],
    [/value is not a valid enumeration member/i, "取值不在允许范围内"],
    [/Internal Server Error/i, "服务器内部错误，请稍后重试"],
    [/Not Found/i, "资源不存在"],
  ];
  for (const [pattern, replacement] of rules) {
    if (pattern.test(message)) return message.replace(pattern, replacement);
  }
  return message;
}

export function toMessage(value: unknown, fallback: string): string {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed && trimmed !== "[object Object]" && trimmed !== "[object Undefined]" && trimmed !== "[object Null]") return trimmed;
    return fallback;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const message = toMessage(item, "");
      if (message) return message;
    }
    return fallback;
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["msg", "message", "detail", "error"] as const) {
      if (typeof record[key] === "string" && record[key].trim()) return record[key];
    }
    if (record.error && typeof record.error === "object") {
      const nested = toMessage(record.error, "");
      if (nested) return nested;
    }
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit, timeoutMs = 12_000): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${CONTROL_ROOT}${path}`, {
      ...init,
      signal: controller.signal,
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = toMessage(payload && typeof payload === "object" ? (payload as Record<string, unknown>).detail : undefined, `API 请求失败（${response.status}）`);
      throw new Error(zhApiError(detail));
    }
    return payload as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

export function readableConsoleError(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") return "请求超时，请检查 API 服务状态。";
  if (error instanceof Error) return toMessage(error.message, "请求失败，请稍后重试。");
  return "控制平面请求失败。";
}

export function getControlOverview(): Promise<ControlOverview> {
  return request<ControlOverview>("/overview");
}

export function getControlJobs(): Promise<{ items: ControlJob[]; total: number }> {
  return request<{ items: ControlJob[]; total: number }>("/jobs");
}

export interface ProviderLedgerItem { ledger_id: string; candidate_id: string; record_id: string; provider: string; provider_task_id: string | null; status: string; checkpoint_state: string; error_code: string | null; error_message: string | null; post_attempts: number; poll_attempts: number; updated_at: string | null }

export interface ControlJobDetail { job: ControlJob; run: ProductionRun | null; categories: ControlCategory[]; artifacts: ControlArtifact[]; events: Array<Record<string, unknown>>; site_scan: SiteScanRunResult | null; candidate_pool: Record<string, unknown>; provider_tasks: ProviderLedgerItem[] }

export function getControlJob(jobId: string): Promise<ControlJobDetail> {
  return request(`/jobs/${encodeURIComponent(jobId)}`);
}

export interface LocalReviewCandidate {
  candidate_id: string;
  record_id: string;
  canonical_url: string;
  preview_url: string;
  source_name: string;
  source_brand: string;
  category_group: string;
  source_dimensions: Record<string, number>;
  dimension_unit: string;
  media_sha256: string;
  media_binding_status: string;
  media_binding_confidence: number | null;
  image_role: string;
  layered_scene7: boolean;
  identity_fields: Record<string, unknown>;
  scope_status: string;
  state: string;
  visual_status: string;
  rejection_reason: string | null;
  local_agent_review?: Record<string, unknown> | null;
}

export function getLocalReviewCandidate(jobId: string, candidateId: string): Promise<{ candidate: LocalReviewCandidate; local_agent_enabled: boolean }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(candidateId)}`);
}

export function recordLocalAgentReview(jobId: string, candidateId: string, review: Record<string, unknown>, actor = "local-agent"): Promise<{ status: string; resume_safe: boolean; candidate: LocalReviewCandidate }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(candidateId)}/local-review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review, actor }),
  });
}

export function getControlReviews(): Promise<{ items: ControlReview[]; total: number }> {
  return request<{ items: ControlReview[]; total: number }>("/reviews");
}

export function getSystemStatus(): Promise<SystemStatus> {
  return request<SystemStatus>("/system");
}

export function preflightSite(url: string, live = true): Promise<Record<string, unknown>> {
  // 预检需抓取真实首页（如 roomandboard 首页首字节约 8s），用更长超时。
  return request<Record<string, unknown>>("/site/preflight", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, live }),
  }, 60_000);
}

export interface CreateControlJobInput {
  source_url: string;
  title: string;
  goal: string;
  target_mode: "EXACT_N" | "UP_TO_N" | "ALL";
  target_value: number | null;
  scope: "NEW_ONLY" | "TOTAL_INCLUDING_EXISTING";
  category_allocation: "PER_CATEGORY" | "TOTAL_ACROSS_SELECTED";
  allocation_strategy: "SEQUENTIAL" | "EVEN" | "PROPORTIONAL" | "CUSTOM";
  spillover: "ASK" | "AUTO_IF_EXPLICIT" | "STOP";
  category_ids: string[];
  provider: "OFF" | "lux3d" | "tripo" | "hunyuan";
}

export function createControlJob(input: CreateControlJobInput): Promise<{ job: ControlJob; site: Record<string, unknown> }> {
  return request("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export interface SiteScanRunResult { scan_id: string; site_key: string; job_id?: string | null; source_url: string; status: string; live: boolean; taxonomy_level: string; brain_status: string; provider_posts: number; browser_session_id?: string | null; error_code?: string | null; error_message?: string | null; result?: { snapshot_id?: string; verified?: boolean; category_count?: number }; started_at: string; finished_at?: string | null }

export function scanControlTaxonomy(jobId: string, live = true): Promise<SiteScanRunResult> {
  return request<SiteScanRunResult>(`/jobs/${encodeURIComponent(jobId)}/taxonomy/scan`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ live }) });
}

export function getSiteScan(scanId: string): Promise<SiteScanRunResult> { return request<SiteScanRunResult>(`/scans/${encodeURIComponent(scanId)}`); }
export function resumeSiteScan(scanId: string): Promise<SiteScanRunResult> { return request<SiteScanRunResult>(`/scans/${encodeURIComponent(scanId)}/resume`, { method: "POST" }); }

export function updateControlTarget(jobId: string, input: { action: "ADD_CATEGORY" | "ACCEPT_SHORTAGE" | "MODIFY_TARGET" | "STOP"; category_ids?: string[]; target_value?: number; reason?: string }): Promise<{ job: ControlJob }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/target`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function approveControlJob(jobId: string, input: { confirm: boolean; approved_cost_ceiling_minor: number; actor: string }): Promise<{ status: string; provider_calls: number; job: ControlJob }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function actOnReview(reviewId: string, input: { action: "CONFIRM" | "EDIT" | "REJECT" | "REQUEST_RESCAN" | "ACCEPT" | "STOP"; reason: string; actor: string }): Promise<Record<string, unknown>> {
  return request(`/reviews/${encodeURIComponent(reviewId)}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

// ---- 网站库与交付 ----
export interface ControlSiteCategory {
  category_id: string;
  native_name: string;
  canonical_name: string;
  path: string;
  source_url?: string;
  count_value?: number | null;
  count_kind?: "EXACT" | "ESTIMATED" | "UNKNOWN" | string;
  confidence?: number;
  reported_count: number;
  eligible_count: number;
  selected: boolean;
  level?: number;
  parent_path?: string | null;
  last_scanned_at: string | null;
}

export interface ControlSite {
  site_key: string;
  domain: string;
  display_name: string;
  source_kind: string;
  status: string;
  profile_version: string;
  category_count: number | null;
  reported_total: number | null;
  known_reported_total?: number;
  unknown_count_categories?: number | null;
  taxonomy_state?: string;
  taxonomy_available?: boolean;
  count_state?: "EXACT" | "ESTIMATED" | "UNKNOWN" | string;
  current_snapshot_id?: string | null;
  eligible_total: number;
  source_url: string;
  job_count: number;
  running_jobs: number;
  pending_review_jobs: number;
  delivered_count: number;
  last_scanned_at: string | null;
  latest_scan_id?: string | null;
  latest_scan_status?: string | null;
  latest_scan_finished_at?: string | null;
  latest_scan_error_code?: string | null;
  latest_scan_error_message?: string | null;
  categories: ControlSiteCategory[];
  updated_at: string | null;
}

export interface ControlSiteDetail {
  schema_version: string;
  site: Record<string, unknown>;
  profile: Record<string, unknown> | null;
  entry_urls: Array<Record<string, unknown>>;
  categories: ControlCategory[];
  taxonomy_state?: string;
  taxonomy_available?: boolean;
  count_state?: "EXACT" | "ESTIMATED" | "UNKNOWN" | string;
  current_snapshot_id?: string | null;
  known_reported_total?: number;
  reported_total?: number | null;
  unknown_count_categories?: number | null;
  snapshots: Array<Record<string, unknown>>;
  scans: Array<Record<string, unknown>>;
  jobs: ControlJob[];
}

export interface ControlDeliveryBatch {
  artifact_id?: string;
  name: string;
  file_count: number;
  download_path: string;
  sha256?: string | null;
  size_bytes?: number;
  manifest_schema?: string | null;
}

export interface ControlDelivery {
  delivery_id: string;
  job_id?: string;
  relative_path: string;
  batch_count: number;
  model_count: number;
  modified_at: string;
  batches: ControlDeliveryBatch[];
}

export function getControlSites(): Promise<{ items: ControlSite[]; total: number }> {
  return request<{ items: ControlSite[]; total: number }>("/sites");
}

export function getControlSite(siteKey: string): Promise<ControlSiteDetail> {
  return request<ControlSiteDetail>(`/sites/${encodeURIComponent(siteKey)}`);
}

export async function scanSite(url: string, live = true): Promise<SiteScanRunResult> {
  const siteKey = (() => { try { return new URL(url).hostname.replace(/^www\./i, ""); } catch { return "unknown-site"; } })();
  return request<SiteScanRunResult>(`/sites/${encodeURIComponent(siteKey)}/scan`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, live }) });
}

export function deleteControlSite(siteKey: string): Promise<{ deleted: boolean; site_key: string; deleted_rows: Record<string, number>; kept_jobs: number }> {
  return request(`/sites/${encodeURIComponent(siteKey)}`, { method: "DELETE" });
}

export function getControlDeliveries(): Promise<{ items: ControlDelivery[]; total: number }> {
  return request<{ items: ControlDelivery[]; total: number }>("/deliveries");
}

export interface ProductionRun {
  run_id: string;
  job_id: string;
  status: string;
  stage: string;
  progress_note: string | null;
  items_done: number;
  items_total: number;
  exit_code: number | null;
  workspace: string | null;
  stdout_tail: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export function startProduction(jobId: string): Promise<{ started: boolean; reason?: string | null; run: ProductionRun | null }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resume_browser_session: true }),
  });
}

export function resumeProduction(jobId: string): Promise<{ started: boolean; reason?: string | null; run: ProductionRun | null }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST" });
}

export function cancelProduction(jobId: string): Promise<{ cancelled: boolean; run: ProductionRun | null }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

export function getProductionRun(jobId: string): Promise<{ run: ProductionRun | null }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/run`);
}

export function deleteControlJob(jobId: string): Promise<{ deleted: boolean; job_id: string }> {
  return request(`/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
}

export function updateControlJob(jobId: string, input: { title?: string; goal?: string; target_value?: number; provider?: string }): Promise<{ job: ControlJob }> {
  return request(`/jobs/${encodeURIComponent(jobId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
