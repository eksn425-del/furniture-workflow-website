"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { API_ROOT } from "@/lib/api";
import {
  actOnReview,
  approveControlJob,
  createControlJob,
  deleteControlJob,
  deleteControlSite,
  getControlDeliveries,
  getControlJob,
  getControlJobs,
  getControlOverview,
  getControlSites,
  getControlSite,
  scanSite,
  getControlReviews,
  getProductionRun,
  getSystemStatus,
  preflightSite,
  resumeSiteScan,
  readableConsoleError,
  scanControlTaxonomy,
  startProduction,
  toMessage,
  updateControlJob,
  updateControlTarget,
  type ControlCategory,
  type ControlDelivery,
  type ControlJob,
  type ProductionRun,
  type ControlSite,
  type ControlSiteDetail,
  type ControlOverview,
  type ControlReview,
  type SystemStatus,
} from "@/lib/console-api";

const STATUS_LABELS: Record<string, string> = {
  DRAFT: "草稿",
  QUEUED: "排队中",
  SITE_SCAN_QUEUED: "扫描已排队",
  TAXONOMY_READY: "类目已就绪",
  POLICY_READY: "策略待确认",
  READY_POOL: "Ready Pool 已保存",
  PRODUCTION_READY: "生产已放行",
  RUNNING: "运行中",
  DISCOVERING: "扫描中",
  WAITING_REVIEW: "等待复核",
  HUMAN_REQUIRED: "需要人工",
  ROBOTS_DENIED: "robots.txt 拒绝",
  TEMPORARY_FAILURE: "临时故障",
  ACCESS_CHANGE_REQUIRED: "访问条件待处理",
  SESSION_CONTINUITY_BROKEN: "会话待恢复",
  BROWSER_REQUIRED: "升级可见浏览器",
  UNVERIFIED: "未验证",
  TARGET_SHORTAGE: "目标不足",
  PROVIDER_RUNNING: "Provider 处理中",
  PRODUCTION_BLOCKED: "生产已阻断",
  BLOCKED: "流程已暂停",
  FAILED: "失败",
  CANCELLED: "已取消",
  COMPLETED: "已交付",
  STOPPED: "已停止",
  APPROVAL_RECORDED: "审批已记录",
  REVIEW_RESOLVED: "复核已处理",
};

const STAGE_LABELS: Record<string, string> = {
  URL_AND_GOAL: "URL 与目标",
  SITE_SCAN: "站点扫描",
  SITE_QUALIFICATION: "站点预检",
  TAXONOMY_SELECTION: "类目选择",
  TARGET_POLICY: "目标策略",
  DISCOVERY: "产品发现",
  MEDIA: "媒体与语义",
  BRAIN_DECISION: "Website Brain 决策",
  DIMENSION: "尺寸与命名",
  READY_POOL: "Ready Pool",
  PROVIDER: "Provider",
  DELIVERY: "交付",
  COMPLETED: "完成",
  PRODUCTION_PLAN: "生产计划",
  PROVIDER_SAFETY_GATE: "Provider 安全闸门",
  STOPPED: "已停止",
};

const ACTIVE_SITE_SCAN_STATUSES = new Set(["QUEUED", "ANALYZING", "L2_BROWSER"]);
const BLOCKED_SITE_SCAN_STATUSES = new Set(["HUMAN_REQUIRED", "ROBOTS_DENIED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "BROWSER_REQUIRED", "BROWSER_RUNTIME_NOT_INSTALLED", "FAILED", "BRAIN_NOT_CONFIGURED"]);

function label(value: string | null | undefined, fallback = "未标记") {
  if (!value) return fallback;
  return STATUS_LABELS[value] ?? STAGE_LABELS[value] ?? value.replaceAll("_", " ");
}

function tone(status: string) {
  if (["COMPLETED", "APPROVAL_RECORDED", "TAXONOMY_READY", "POLICY_READY", "READY_POOL", "PRODUCTION_READY", "REVIEW_RESOLVED"].includes(status)) return "success";
  if (["QUEUED", "SITE_SCAN_QUEUED", "HUMAN_REQUIRED", "WAITING_REVIEW", "TARGET_SHORTAGE", "UNVERIFIED"].includes(status)) return "attention";
  if (["FAILED", "BLOCKED", "CANCELLED", "PRODUCTION_BLOCKED", "STOPPED"].includes(status)) return "danger";
  return "active";
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function taxonomyCountLabel(countKind: string | undefined, countValue: number | null | undefined) {
  if (countKind === "EXACT" && countValue !== null && countValue !== undefined) return `${countValue} 件 · 直接证据`;
  if (countKind === "ESTIMATED" && countValue !== null && countValue !== undefined) return `约 ${countValue} 件 · 页面样本`;
  return "数量未知 · UNKNOWN";
}

function siteScanBlocker(site: ControlSite) {
  const status = String(site.latest_scan_status ?? site.status ?? "");
  const detail = String(site.latest_scan_error_message ?? "").trim();
  const reason = detail ? `（${detail}）` : "";
  if (status === "HUMAN_REQUIRED") return `最新扫描需要人工验证${reason}请回网站库点击“恢复同一扫描”，验证通过后再补全类目数量。`;
  if (status === "ROBOTS_DENIED") return `最新扫描被 robots.txt 拒绝${reason}这不是人机验证；请确认官方导出/授权接口，或停止当前扫描。`;
  if (status === "TEMPORARY_FAILURE") return `最新扫描遇到临时页面或导航故障${reason}同一浏览器会话和检查点已保留，可直接恢复重试。`;
  if (status === "ACCESS_CHANGE_REQUIRED") return `最新扫描在可见浏览器中仍被拒绝，但没有验证控件${reason}请检查网络或站点访问条件后恢复。`;
  if (status === "SESSION_CONTINUITY_BROKEN") return `最新扫描的持久浏览器会话中断${reason}请恢复同一扫描，不要新建重复任务。`;
  if (status === "BROWSER_RUNTIME_NOT_INSTALLED") return `最新扫描缺少 Website 原生浏览器运行时${reason}请先安装运行时后重新扫描。`;
  if (status === "FAILED" || status === "BROWSER_REQUIRED") return `最新扫描未完成（${label(status)}）${reason}请回网站库重新摸底。`;
  if (status === "BRAIN_NOT_CONFIGURED") return `最新扫描需要配置 Website Brain${reason}请配置 WEBSITE_BRAIN_* 后恢复扫描。`;
  return "当前没有可用的最新类目快照，请重新摸底。";
}

function pathVariants(value: string) {
  let path = value;
  try { path = new URL(value).pathname; } catch { /* 已经是路径 */ }
  path = `/${path.split("?")[0].split("#")[0].replace(/^\/+|\/+$/g, "")}`;
  const variants = new Set([path || "/"]);
  const prefixes = new Set(["catalog", "shop", "collections", "categories"]);
  let current = path;
  while (current !== "/") {
    const parts = current.split("/").filter(Boolean);
    if (!parts.length || !prefixes.has(parts[0].toLowerCase())) break;
    current = parts.length > 1 ? `/${parts.slice(1).join("/")}` : "/";
    variants.add(current);
  }
  return variants;
}

function isRootUrl(value: string) {
  try { return ["", "/"].includes(new URL(value).pathname); } catch { return false; }
}

function scopedCategoriesForUrl<T extends { path: string; source_url?: string; parent_path?: string | null }>(categories: T[], sourceUrl: string) {
  const targets = pathVariants(sourceUrl);
  const matches = categories.filter((category) => {
    const candidates = new Set([...pathVariants(category.path), ...(category.source_url ? pathVariants(category.source_url) : [])]);
    return [...targets].some((target) => candidates.has(target));
  });
  if (!matches.length) return [];
  const matchedPaths = new Set(matches.map((category) => category.path));
  return categories.filter((category) => matchedPaths.has(category.path) || (category.parent_path ? matchedPaths.has(category.parent_path) : false));
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`console-status console-status--${tone(status)}`}><i />{label(status)}</span>;
}

function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return (
    <div className="console-page-header">
      <div>
        <p className="console-eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="console-page-description">{description}</p>
      </div>
      {action ? <div className="console-header-actions">{action}</div> : null}
    </div>
  );
}

function SectionTitle({ title, detail, action }: { title: string; detail?: string; action?: React.ReactNode }) {
  return <div className="console-section-title"><div><h2>{title}</h2>{detail ? <p>{detail}</p> : null}</div>{action}</div>;
}

function ActionCard({ label: cardLabel, value, description, href, variant }: { label: string; value: number; description: string; href: string; variant: string }) {
  return <Link href={href} className={`console-action-card console-action-card--${variant}`}><span className="console-card-kicker">{cardLabel}</span><strong>{formatNumber(value)}</strong><span>{description}</span><b>查看队列 <span aria-hidden="true">↗</span></b></Link>;
}

function EmptyJobs({ compact = false }: { compact?: boolean }) {
  return <div className={`console-empty ${compact ? "console-empty--compact" : ""}`}><div className="console-empty-mark">∅</div><div><strong>还没有真实生产任务</strong><p>创建一个 Job 后，站点、目标、证据和交付进度会在这里持续更新。</p></div>{!compact ? <Link className="console-button console-button--secondary" href="/jobs/new">创建第一个 Job</Link> : null}</div>;
}

export function ProductionDashboard() {
  const [overview, setOverview] = useState<ControlOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [selectedJob, setSelectedJob] = useState<ControlJob | null>(null);

  async function refresh() {
    setBusy(true);
    setError(null);
    try { setOverview(await getControlOverview()); } catch (reason) { setError(readableConsoleError(reason)); } finally { setBusy(false); }
  }

  useEffect(() => {
    let active = true;
    getControlOverview().then((payload) => { if (active) setOverview(payload); }).catch((reason) => { if (active) setError(readableConsoleError(reason)); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);
  const cards = overview?.action_cards ?? { running: 0, waiting_review: 0, human_required: 0, failed: 0, provider_running: 0, delivered_today: 0 };
  const metrics = overview?.metrics ?? { today_output: 0, success_rate: 0, average_duration_hours: 0, cost_minor: 0, workers_online: 0 };

  return <div className="console-page">
    <PageHeader eyebrow="生产控制台 / 今日工作台" title="今天的生产面板" description="先看需要动作的任务，再进入产品、证据和交付。所有外部采集与 Provider 操作都受 Job Policy 和安全闸门约束。" action={<><button className="console-button console-button--quiet" type="button" onClick={() => void refresh()} disabled={busy}>{busy ? "读取中…" : "刷新状态"}</button><Link className="console-button console-button--primary" href="/jobs/new"><span aria-hidden="true">＋</span> 新建任务</Link></>} />
    {error ? <div className="console-alert console-alert--danger"><strong>控制平面暂不可用</strong><span>{error}</span><button type="button" onClick={() => void refresh()}>重新连接</button></div> : null}
    <section className="console-action-grid" aria-label="需要动作的任务">
      <ActionCard label="运行中" value={cards.running} description="自动推进中的 Job" href="/jobs?status=RUNNING" variant="teal" />
      <ActionCard label="等待复核" value={cards.waiting_review} description="需要确认或查看证据" href="/review" variant="amber" />
      <ActionCard label="需要人工" value={cards.human_required} description="验证码、登录或策略选择" href="/review?reason=HUMAN_REQUIRED" variant="purple" />
      <ActionCard label="有异常" value={cards.failed} description="已停止自动推进" href="/jobs?status=FAILED" variant="red" />
      <ActionCard label="Provider 中" value={cards.provider_running} description="等待模型或下载结果" href="/jobs?status=PROVIDER_RUNNING" variant="blue" />
      <ActionCard label="今日交付" value={cards.delivered_today} description="已通过交付闸门的产品" href="/delivery" variant="green" />
    </section>
    <section className="console-metric-strip" aria-label="今日指标">
      <div><span>今日产出</span><strong>{formatNumber(metrics.today_output)}</strong><small>件</small></div>
      <div><span>任务成功率</span><strong>{metrics.success_rate}%</strong><small>按 Job 统计</small></div>
      <div><span>平均时长</span><strong>{metrics.average_duration_hours || "—"}</strong><small>{metrics.average_duration_hours ? "小时" : "数据积累中"}</small></div>
      <div><span>已记账成本</span><strong>{metrics.cost_minor ? `¥${(metrics.cost_minor / 100).toFixed(2)}` : "¥0.00"}</strong><small>Provider calls</small></div>
      <div><span>Worker 在线</span><strong>{metrics.workers_online || "—"}</strong><small>{metrics.workers_online ? "个" : "等待心跳"}</small></div>
    </section>
    <div className="console-main-grid">
      <section className="console-panel console-panel--wide"><SectionTitle title="需要关注的任务" detail="按最近更新时间排列；状态和下一步使用业务语言显示。" action={<Link href="/jobs" className="console-inline-link">全部任务 →</Link>} />
        {overview?.jobs?.length ? <div className="console-table-wrap"><table className="console-table"><thead><tr><th>任务</th><th>站点</th><th>目标 / 当前</th><th>阶段</th><th>状态</th><th>更新</th><th /></tr></thead><tbody>{overview.jobs.map((job) => <tr key={job.job_id} onClick={() => setSelectedJob(job)}><td><strong>{job.title}</strong><small>{job.goal}</small></td><td>{job.site_name}<small>{job.site_key}</small></td><td><b>{job.target_mode === "ALL" ? "全部" : job.target_value ?? "—"}</b><small>{job.counts.eligible_count} 合格 · {job.counts.delivered_count} 已交付</small></td><td>{label(job.current_stage)}<small>{job.last_reason ?? "—"}</small></td><td><StatusBadge status={job.status} /></td><td className="console-nowrap">{formatTime(job.updated_at)}</td><td><button className="console-row-action" type="button" onClick={(event) => { event.stopPropagation(); setSelectedJob(job); }}>打开</button></td></tr>)}</tbody></table></div> : <EmptyJobs />}
      </section>
      <section className="console-panel"><SectionTitle title="Review Center" detail="按人工动作聚类" action={<Link href="/review" className="console-inline-link">打开 →</Link>} />
        {overview?.reviews?.length ? <div className="console-review-list">{overview.reviews.slice(0, 5).map((review) => <Link href="/review" key={review.review_id} className="console-review-row"><span className={`review-severity review-severity--${review.severity.toLowerCase()}`}>{review.severity}</span><div><strong>{review.title}</strong><p>{review.detail}</p><small>{formatTime(review.created_at)}</small></div><span aria-hidden="true">›</span></Link>)}</div> : <EmptyJobs compact />}
      </section>
    </div>
    <div className="console-lower-grid">
      <section className="console-panel"><SectionTitle title="Provider 队列" detail="提交安全与容量分开显示" action={<Link href="/system" className="console-inline-link">系统 →</Link>} /><div className="queue-summary"><div><span>运行中</span><strong>{overview?.provider_queue.running ?? 0}</strong></div><div><span>等待中</span><strong>{overview?.provider_queue.waiting ?? 0}</strong></div><div><span>容量</span><strong>{overview?.provider_queue.capacity || "—"}</strong></div></div><div className="console-note"><i className="signal-dot signal-dot--green" /> Provider 默认关闭；未通过幂等性与 SUBMISSION_UNKNOWN 闸门前，不会显示“开始付费生成”。</div></section>
      <section className="console-panel"><SectionTitle title="系统健康" detail="3 秒内判断能否继续工作" action={<Link href="/system" className="console-inline-link">查看详情 →</Link>} /><div className="health-rows"><HealthRow label="Website API" value={error ? "连接异常" : "在线"} tone={error ? "danger" : "success"} /><HealthRow label="Website Brain" value="按需配置" tone="attention" /><HealthRow label="Provider Safety" value="未启用付费调用" tone="success" /><HealthRow label="对象存储" value="开发目录" tone="attention" /></div></section>
    </div>
    {selectedJob ? <JobDrawer job={selectedJob} onClose={() => setSelectedJob(null)} /> : null}
  </div>;
}

function HealthRow({ label: rowLabel, value, tone: rowTone }: { label: string; value: string; tone: string }) { return <div className="health-row"><span><i className={`signal-dot signal-dot--${rowTone}`} />{rowLabel}</span><strong>{value}</strong></div>; }

function JobDrawer({ job, onClose }: { job: ControlJob; onClose: () => void }) {
  const [detail, setDetail] = useState<{ categories: ControlCategory[]; events: Array<Record<string, unknown>> } | null>(null);
  useEffect(() => { void getControlJob(job.job_id).then((response) => setDetail({ categories: response.categories, events: response.events })).catch(() => undefined); }, [job.job_id]);
  return <div className="console-drawer-backdrop" role="presentation" onMouseDown={onClose}><aside className="console-drawer" role="dialog" aria-modal="true" aria-label="任务详情" onMouseDown={(event) => event.stopPropagation()}><div className="console-drawer-head"><div><p className="console-eyebrow">Job Detail</p><h2>{job.title}</h2></div><button type="button" className="console-close" onClick={onClose} aria-label="关闭">×</button></div><div className="drawer-status-line"><StatusBadge status={job.status} /><span>{job.site_name} · {label(job.current_stage)}</span></div><div className="drawer-counts"><div><span>目标</span><strong>{job.target_mode === "ALL" ? "全部" : job.target_value ?? "—"}</strong></div><div><span>合格</span><strong>{job.counts.eligible_count}</strong></div><div><span>已交付</span><strong>{job.counts.delivered_count}</strong></div></div><div className="drawer-block"><h3>下一步</h3><p>{job.last_reason ?? "等待系统更新"}</p></div><div className="drawer-block"><h3>类目</h3>{detail?.categories?.length ? <div className="drawer-category-list">{detail.categories.map((category) => <div key={category.category_id}><span>{category.canonical_name}</span><small>{taxonomyCountLabel(category.count_kind, category.count_value)}</small></div>)}</div> : <p className="drawer-muted">正在读取类目快照…</p>}</div><div className="drawer-block"><h3>安全</h3><div className="drawer-safety"><span>Provider</span><b>{job.provider === "OFF" ? "OFF" : job.provider}</b><span>Safety Gate</span><b>{job.provider_safety}</b><span>外部调用</span><b>{job.provider_calls}</b></div></div><Link className="console-button console-button--primary console-button--full" href={`/jobs/${encodeURIComponent(job.job_id)}`}>打开完整 Job Detail</Link></aside></div>;
}

export function NewJobWizard() {
  const [step, setStep] = useState(1);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [targetMode, setTargetMode] = useState<"EXACT_N" | "UP_TO_N" | "ALL">("EXACT_N");
  const [targetValue, setTargetValue] = useState(3);
  const [scope, setScope] = useState<"NEW_ONLY" | "TOTAL_INCLUDING_EXISTING">("NEW_ONLY");
  const [allocation, setAllocation] = useState<"TOTAL_ACROSS_SELECTED" | "PER_CATEGORY">("TOTAL_ACROSS_SELECTED");
  const [strategy, setStrategy] = useState<"SEQUENTIAL" | "EVEN" | "PROPORTIONAL" | "CUSTOM">("SEQUENTIAL");
  const [spillover, setSpillover] = useState<"ASK" | "AUTO_IF_EXPLICIT" | "STOP">("ASK");
  const [preflight, setPreflight] = useState<Record<string, unknown> | null>(null);
  const [job, setJob] = useState<ControlJob | null>(null);
  const [categories, setCategories] = useState<ControlCategory[]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [blocker, setBlocker] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);
  const scanTimer = useRef<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sites, setSites] = useState<ControlSite[]>([]);
  const [run, setRun] = useState<ProductionRun | null>(null);
  // Provider 安全闸门：建模/生成收费调用前必须显式选择并保存（OFF / Lux3D）。
  const [provider, setProvider] = useState<string>("OFF");
  // 内嵌环境不支持 window.prompt()，成本上限改用应用内输入行采集。
  const [askCeiling, setAskCeiling] = useState(false);
  const [ceilingRaw, setCeilingRaw] = useState("100");
  // 步骤2两级类目树：记录已展开的一级类目路径。
  const [expandedGroups, setExpandedGroups] = useState<string[]>([]);

  // 从网站库进入时只恢复服务器持久化类目，不在浏览器存运行时草稿。
  function ensureJobForUrl(sourceUrl: string, note = "已读取当前类目快照；数量以每行的 EXACT / ESTIMATED / UNKNOWN 状态为准。勾选并到下一步保存后才会创建任务。") {
    try {
      const host = new URL(sourceUrl).hostname.replace(/^www\./i, "");
      setTitle((current) => current.trim() ? current : `${host} 网站采集`);
      setGoal((current) => current.trim() ? current : "按用户选择的类目扫描公开产品证据并准备交付");
      setMessage(`${host}：${note}`);
    } catch { /* 仅填充草稿，失败不阻塞类目选择 */ }
  }

  useEffect(() => {
    void getControlSites().then(async (response) => {
      setSites(response.items);
      const fromUrl = new URLSearchParams(window.location.search).get("url");
      // 从「网站总览」点“开始任务”进入：该网站已在库且已摸底过，直接读其已验证类目到 02 类目选择，
      // 不再回退到 01 网址摸底（避免重复摸底）。
      if (fromUrl) {
        const host = (() => { try { return new URL(fromUrl).hostname.replace(/^www\./i, "").toLowerCase(); } catch { return ""; } })();
        const norm = (value: string) => String(value).replace(/^www\./i, "").toLowerCase();
        const matchedSite = response.items.find((site) => !!host && norm(site.domain) === host);
        const latestStatus = String(matchedSite?.latest_scan_status ?? "");
        const matched = response.items.find((site) => !!host && norm(site.domain) === host
          && site.taxonomy_available !== false
          && !ACTIVE_SITE_SCAN_STATUSES.has(String(site.latest_scan_status ?? ""))
          && (site.categories?.length ?? 0) > 0);
        if (matchedSite && BLOCKED_SITE_SCAN_STATUSES.has(latestStatus)) {
          setUrl(fromUrl);
          setBlocker(siteScanBlocker(matchedSite));
          setMessage(`${host} 的旧类目证据仍保留在网站档案中，但当前不能当作最新已验证结果使用。`);
          return;
        }
        if (matched && isRootUrl(fromUrl)) {
          setUrl(fromUrl);
          // 站点列表里的类目是精简视图，补齐 ControlCategory 所需字段再进「类目选择」。
          setCategories(matched.categories.map((category) => ({
            category_id: category.category_id,
            site_key: matched.site_key,
            native_name: category.native_name,
            canonical_name: category.canonical_name,
            path: category.path,
            source_url: category.source_url ?? "",
            count_value: category.count_value ?? null,
            count_kind: category.count_kind ?? "UNKNOWN",
            confidence: category.confidence ?? 0,
            evidence: [],
            verified_at: null,
            reported_count: category.reported_count,
            discovered_count: category.reported_count,
            eligible_count: category.eligible_count,
            selected: category.selected,
            level: category.level,
            parent_path: category.parent_path ?? null,
            last_scanned_at: category.last_scanned_at,
          })));
          await ensureJobForUrl(fromUrl, latestStatus === "PARTIAL"
            ? "已读取部分类目；部分商品数量证据尚未补全，行内会明确标注 UNKNOWN。"
            : undefined);
          setStep(2);
          return;
        }
        if (matched) {
          // 具体类目 URL 是操作员明确给出的 scope，不能偷换成整个站点的
          // 根类目；只有已有快照中能证明该 scope 时才直接复用。
          const scoped = scopedCategoriesForUrl(matched.categories, fromUrl);
          if (scoped.length > 0) {
            setUrl(fromUrl);
            setCategories(scoped.map((category) => ({
              category_id: category.category_id,
              site_key: matched.site_key,
              native_name: category.native_name,
              canonical_name: category.canonical_name,
              path: category.path,
              source_url: category.source_url ?? "",
              count_value: category.count_value ?? null,
              count_kind: category.count_kind ?? "UNKNOWN",
              confidence: category.confidence ?? 0,
              evidence: [],
              verified_at: null,
              reported_count: category.reported_count,
              discovered_count: category.reported_count,
              eligible_count: category.eligible_count,
              selected: category.selected,
              level: category.level,
              parent_path: category.parent_path ?? null,
              last_scanned_at: category.last_scanned_at,
            })));
            await ensureJobForUrl(fromUrl, latestStatus === "PARTIAL"
              ? "已按具体类目范围读取部分快照；数量证据尚未全部补全，行内会明确标注 UNKNOWN。"
              : "已按具体类目范围读取当前快照，直接进入类目选择。保存前不会创建任务。");
            setStep(2);
            return;
          }
          // 站点已在库，但这个具体 scope 尚未有快照：留在步骤 1，
          // 让操作员点击“预检并扫描类目”走一次真实的 scope-first 扫描。
          setUrl(fromUrl);
          await ensureJobForUrl(fromUrl, "这是具体类目入口，当前快照没有该范围；点击下方按钮重新取证，避免混入全站类目。");
          return;
        }
        // 网站库列表拿到的站点没有可验证类目（如曾被反爬清空），或列表本身超时/为空：
        // 只要库里已有该站且带已验证类目，就按 host 直接拉站点详情跳到 02 类目选择，
        // 不把已导入站点打回 01 网址摸底。
        if (host) {
          try {
            const detail = await getControlSite(host);
            const detailCategories = Array.isArray(detail?.categories) ? detail.categories : [];
            const scopedDetailCategories = fromUrl && !isRootUrl(fromUrl)
              ? scopedCategoriesForUrl(detailCategories, fromUrl)
              : detailCategories;
            if (scopedDetailCategories.length > 0) {
              const siteUrl = fromUrl && !isRootUrl(fromUrl) ? fromUrl : String(detail.site?.source_url ?? `https://${host}`);
              setUrl(siteUrl);
              setCategories(scopedDetailCategories);
              await ensureJobForUrl(siteUrl, fromUrl && !isRootUrl(fromUrl)
                ? "已按具体类目范围读取快照，直接进入类目选择。保存前不会创建任务。"
                : undefined);
              setStep(2);
              return;
            }
            if (fromUrl && !isRootUrl(fromUrl) && detailCategories.length > 0) {
              setUrl(fromUrl);
              await ensureJobForUrl(fromUrl, "这是具体类目入口，当前快照没有该范围；点击下方按钮重新取证，避免混入全站类目。");
              return;
            }
          } catch { /* 拉取失败则回退到普通摸底表单 */ }
        }
      }
      if (fromUrl) setUrl(fromUrl);
    }).catch(() => undefined);
    return () => { if (scanTimer.current !== null) { window.clearTimeout(scanTimer.current); scanTimer.current = null; } };
  }, []);

  // 扫描到达终态：写回类目并进入「类目选择」步骤。
  function finishScan(cats: ControlCategory[], finalJob: ControlJob, weakEvidence: boolean) {
    setScanning(false);
    setCategories(cats);
    setSelectedCategories([]);
    setJob(finalJob);
    setProvider(String(finalJob.provider ?? "OFF"));
    setMessage(null);
    if (weakEvidence) setBlocker("扫描已完成但类目证据不足，请进入人工复核。");
    else if (cats.some((category) => category.count_kind === "UNKNOWN")) setBlocker("类目已发现，但商品数量证据尚未补全；UNKNOWN 不代表 0。完成验证或重新摸底后，Website 会继续 L2 数量补证。");
    setStep(2);
  }

  function handleUrlChange(value: string) {
    setUrl(value);
  }

  // 步骤1：输入网址 → 预检 + 建任务 + 扫描类目。类目扫描放到后台运行，
  // 前端先用短超时触发，若未完成则转轮询，避免长时间锁死页面。
  async function probeAndScan() {
    if (!/^https?:\/\/.+/i.test(url.trim())) { setBlocker("网址格式不对，请输入类似 https://example.com 的完整网址"); return; }
    if (scanTimer.current !== null) { window.clearTimeout(scanTimer.current); scanTimer.current = null; }
    setScanning(false); setBusy(true); setBlocker(null); setMessage(null);
    try {
      const check = await preflightSite(url, true);
      setPreflight(check);
      if (check.status === "INVALID_INPUT") throw new Error(toMessage(check.next_action, "URL 无效"));
      if (check.status === "FAILED") {
        // 站点本身连不上（连接超时/拒绝等）：立刻给出明确原因，不再继续建任务和扫描。
        const blockerInfo = (check as { blocker?: { code?: string; message?: string } }).blocker;
        const code = String(blockerInfo?.code ?? "");
        const hint = /timeout|timed out/i.test(String(blockerInfo?.message ?? "")) || code.includes("TIMEOUT")
          ? "该站点当前无法访问（连接超时）。请检查本机网络或开启代理后重试。"
          : `该站点当前无法访问（${code || "FAILED"}）。请确认网址正确且站点公开可访问。`;
        throw new Error(hint);
      }
      if (check.status === "BROWSER_REQUIRED") setMessage("HTTP 证据不足；将自动升级 Website 原生可见浏览器，这不代表出现了人机验证。");
      if (check.status === "TEMPORARY_FAILURE") setMessage("HTTP 预检遇到临时故障；任务会继续由持久可见浏览器复核。");
      let displayTitle = title.trim();
      let displayGoal = goal.trim();
      try {
        const host = new URL(url).hostname.replace(/^www\./i, "");
        if (!displayTitle) displayTitle = `${host} 网站采集`;
        if (!displayGoal) displayGoal = "按用户选择的类目扫描公开产品证据并准备交付";
      } catch { /* URL 校验已在上方完成 */ }
      const created = await createControlJob({ source_url: url, title: displayTitle, goal: displayGoal, target_mode: targetMode, target_value: targetMode === "ALL" ? null : targetValue, scope, category_allocation: allocation, allocation_strategy: strategy, spillover, category_ids: [], provider: "OFF" });
      setJob(created.job);
      const result = await scanControlTaxonomy(created.job.job_id, true);
      setBusy(false);
      setScanning(true);
      setMessage(`站点扫描 ${result.scan_id} 已进入持久化队列。可离开本页，完成后站点库和任务详情会保留结果。`);
      void pollScan(created.job.job_id, 0);
    } catch (reason) { setBlocker(readableConsoleError(reason)); } finally { setBusy(false); }
  }

  async function pollScan(jobId: string, attempt: number) {
    if (attempt >= 120) { setScanning(false); setBlocker("扫描超过约 6 分钟未见终态，请到任务详情页查看扫描状态后重试。"); return; }
    try {
      const detail = await getControlJob(jobId);
      const status = detail.job.status;
      if (status === "TAXONOMY_READY" || status === "WAITING_REVIEW") {
        finishScan(detail.categories, detail.job, status === "WAITING_REVIEW" && detail.categories.length === 0);
        return;
      }
      if (["HUMAN_REQUIRED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "BRAIN_NOT_CONFIGURED"].includes(status)) {
        setScanning(false);
        setJob(detail.job);
        setBlocker(detail.job.last_reason ?? (status === "HUMAN_REQUIRED" ? "可见浏览器会话需要人工处理后恢复" : "请配置 WEBSITE_BRAIN_* 后恢复扫描"));
        return;
      }
    } catch { /* 轮询失败则下轮重试 */ }
    scanTimer.current = window.setTimeout(() => { void pollScan(jobId, attempt + 1); }, 3000);
  }

  // 步骤3：保存目标数量 + 类目。若尚无可复用 Job（从网站总览进入、只在浏览类目），
  // 这里才真正创建任务；已存在 Job 则沿用原有更新路径。
  async function savePolicy() {
    if (selectedCategories.length === 0) return;
    setBusy(true); setMessage(null); setBlocker(null);
    try {
      let current = job;
      if (!current) {
        let displayTitle = title.trim();
        let displayGoal = goal.trim();
        const host = (() => { try { return new URL(url).hostname.replace(/^www\./i, ""); } catch { return url; } })();
        if (!displayTitle) displayTitle = `${host} 网站采集`;
        if (!displayGoal) displayGoal = "按用户选择的类目扫描公开产品证据并准备交付";
        const created = await createControlJob({ source_url: url, title: displayTitle, goal: displayGoal, target_mode: targetMode, target_value: targetMode === "ALL" ? null : targetValue, scope, category_allocation: allocation, allocation_strategy: strategy, spillover, category_ids: selectedCategories, provider: "OFF" });
        current = created.job;
        setMessage(`任务已创建：${current.title}`);
      } else {
        let updated = current;
        if (targetMode !== "ALL") {
          const res = await updateControlTarget(current.job_id, { action: "MODIFY_TARGET", target_value: targetValue, reason: "操作员在目标与策略步骤确认目标数量" });
          updated = res.job;
        }
        const saved = await updateControlTarget(updated.job_id, { action: "ADD_CATEGORY", category_ids: selectedCategories, reason: "操作员在目标与策略步骤确认类目" });
        current = saved.job;
      }
      setJob(current); setProvider(String(current.provider ?? "OFF")); setStep(4);
    } catch (reason) { setBlocker(readableConsoleError(reason)); } finally { setBusy(false); }
  }

  async function startProductionFlow() {
    if (!job) return;
    setBlocker(null);
    // 关闭 Provider（OFF）时无需审批成本上限，直接启动；付费 Provider 先用应用内输入行采集成本上限。
    if (provider !== "OFF" && job.provider_safety !== "PRODUCTION_READY") {
      setAskCeiling(true);
      return;
    }
    await commitProductionStart(job.job_id);
  }

  // 内嵌环境不支持 window.prompt()，成本上限由应用内输入行提交后走审批→启动。
  async function confirmPaywallCeiling() {
    if (!job) return;
    setBusy(true); setMessage(null); setBlocker(null);
    try {
      const ceilingMinor = Math.round(Number(ceilingRaw) * 100);
      if (!Number.isFinite(ceilingMinor) || ceilingMinor <= 0) throw new Error("成本上限必须是大于 0 的金额");
      const approval = await approveControlJob(job.job_id, { confirm: true, approved_cost_ceiling_minor: ceilingMinor, actor: "website-operator" });
      setJob(approval.job);
      if (approval.status !== "PRODUCTION_READY") throw new Error(approval.job.last_reason ?? "Provider qualification 未通过");
      setAskCeiling(false);
      await commitProductionStart(job.job_id);
    } catch (reason) { setBlocker(readableConsoleError(reason)); } finally { setBusy(false); }
  }

  async function commitProductionStart(jobId: string) {
    setBusy(true); setMessage(null); setBlocker(null);
    try {
      const started = await startProduction(jobId);
      setRun(started.run);
      if (!started.started) {
        setMessage(started.reason === "ALREADY_RUNNING" ? "该任务已有生产在运行，继续显示进度。" : "生产未启动，请稍后重试。");
      } else {
        setMessage("生产已启动，正在通过 Website Native Runtime 执行…");
      }
      void pollRun();
    } catch (reason) { setBlocker(readableConsoleError(reason)); } finally { setBusy(false); }
  }

  async function pollRun() {
    if (!job) return;
    try {
      const response = await getProductionRun(job.job_id);
      const current = response.run;
      if (!current) return;
      setRun(current);
      if (current.status === "RUNNING") {
        window.setTimeout(() => { void pollRun(); }, 4000);
      } else if (current.status === "SUCCEEDED") {
        setMessage("生产完成！可在「交付」页面查看产物。");
      } else if (current.status === "FAILED" || current.status === "BLOCKED") {
        const requiresHuman = current.stage === "L2_BROWSER" || job.status === "HUMAN_REQUIRED";
        setBlocker(requiresHuman ? `需要人工处理（L2 浏览器）：${current.progress_note ?? "请处理可见浏览器页面后恢复同一 Job"}。` : `生产结束（${current.status}）：${current.progress_note ?? "请查看运行日志"}。`);
      }
    } catch { /* 轮询失败则下轮重试 */ }
  }

  // 步骤4：切换 Provider 并在落库保存。Provider 决定建模/生成闸门是否开启，
  // 只有选到 Lux3D（且后端已配置密钥）才会真正产生付费调用。
  async function saveProvider(next: string) {
    setProvider(next);
    if (!job) { setMessage("已记住 Provider 选择，任务创建后生效。"); return; }
    setBusy(true); setBlocker(null);
    try {
      const updated = await updateControlJob(job.job_id, { provider: next });
      setJob(updated.job);
      setMessage(`Provider 已切换为 ${next === "OFF" ? "关闭" : "Lux3D"}（${next === "OFF" ? "不会产生付费建模调用" : "建模闸门开启，开始生产将调用 Lux3D"}）。`);
    } catch (reason) { setBlocker(readableConsoleError(reason)); setProvider(job?.provider ?? "OFF"); }
    finally { setBusy(false); }
  }

  // 两级类目模型：一级 = 站点大类目，二级 = 其子类目；level/parent_path 由 API 返回，
  // 旧数据缺失时按路径深度现算兜底。
  const levelOfCategory = (category: ControlCategory) => category.level ?? (category.path.split("/").filter(Boolean).length >= 2 ? 2 : 1);
  const parentOfCategory = (category: ControlCategory) => category.parent_path ?? (() => { const segments = category.path.split("/").filter(Boolean); return segments.length >= 2 ? `/${segments[0]}` : null; })();
  const rootCategories = categories.filter((category) => levelOfCategory(category) === 1);
  // 孤儿二级：父级一级类目被营销词过滤或截断掉时，仍按顶级行展示，保证可选。
  const rootPaths = new Set(rootCategories.map((root) => root.path));
  const topLevelCategories = [...rootCategories, ...categories.filter((category) => levelOfCategory(category) === 2 && !rootPaths.has(parentOfCategory(category) ?? ""))];
  const childCategoriesOf = (root: ControlCategory) => categories.filter((category) => parentOfCategory(category) === root.path);
  const countLabelOf = (category: ControlCategory) => taxonomyCountLabel(category.count_kind, category.count_value);
  // 一级类目若有自身官方数量，必须优先使用，不能用子类目不完整清单的合计覆盖；
  // 只有父级数量缺失时，才把已发现子类目的数量作为“可见合计”兜底。
  const rootTotalOf = (root: ControlCategory) => root.count_value ?? childCategoriesOf(root).reduce((sum, child) => sum + (child.count_value ?? 0), 0);
  const rootCountLabelOf = (root: ControlCategory) => {
    if (root.count_value !== null && root.count_value !== undefined) return countLabelOf(root);
    const visibleTotal = childCategoriesOf(root).reduce((sum, child) => sum + (child.count_value ?? 0), 0);
    return visibleTotal > 0 ? `子类目可见合计 ${visibleTotal} 件` : countLabelOf(root);
  };
  const selectedCount = categories.filter((category) => selectedCategories.includes(category.category_id)).reduce((sum, category) => sum + (levelOfCategory(category) === 1 ? rootTotalOf(category) : (category.count_value ?? 0)), 0);
  const selectedHasUnknownCount = categories.some((category) => selectedCategories.includes(category.category_id) && category.count_kind === "UNKNOWN");
  const selectedCountLabel = selectedCategories.length === 0 ? "未选择" : selectedHasUnknownCount ? "未知" : formatNumber(selectedCount);
  function toggleCategory(categoryId: string) {
    setSelectedCategories((current) => current.includes(categoryId) ? current.filter((id) => id !== categoryId) : [...current, categoryId]);
  }
  const stepNames = ["网址摸底", "类目选择", "目标与策略", "生产计划"];
  return <div className="console-page console-page--narrow"><PageHeader eyebrow="新建任务 / 渐进式配置" title="先摸清网站，再定爬虫目标" description="先输入网址让系统摸底类目和数量，看到清单后再决定爬哪些、要多少。Provider 默认关闭。" action={<Link href="/" className="console-button console-button--quiet">返回 Dashboard</Link>} />
    <div className="wizard-rail" aria-label="新建任务步骤">{stepNames.map((name, index) => <div key={name} className={`wizard-step ${step === index + 1 ? "is-current" : ""} ${step > index + 1 ? "is-done" : ""}`}><span>{step > index + 1 ? "✓" : index + 1}</span><b>{name}</b></div>)}</div>
    {blocker ? <div className="console-alert console-alert--attention"><strong>需要注意</strong><span>{blocker}</span></div> : null}
    {message ? <div className="console-alert console-alert--success"><strong>已记录</strong><span>{message}</span></div> : null}
    <section className="console-panel wizard-panel">
      {step === 1 ? <><SectionTitle title="01 · 网址摸底" detail="输入任意站点 URL，系统先预检并扫描类目，把类目和数量列给你。这一步不会调用 Provider，也不会下载媒体。" /><div className="wizard-form">{sites.length ? <div className="wizard-subsection"><div className="wizard-subsection-head"><h3>从网站库选择</h3><span>已导入的网站可以直接选，不用重新输入网址。</span></div><select className="console-input" value="" onChange={(event) => { const site = sites.find((item) => item.site_key === event.target.value); if (site) { handleUrlChange(site.source_url || `https://${site.domain}`); setTitle(`${site.display_name} 爬虫计划`); } }}><option value="">— 选择一个已导入网站 —</option>{sites.map((site) => <option key={site.site_key} value={site.site_key}>{site.display_name}（{site.domain} · {site.category_count ?? "未知"} 类）</option>)}</select></div> : null}<label className="console-field"><span>公开入口 URL</span><input value={url} onChange={(event) => handleUrlChange(event.target.value)} placeholder="https://example.com/category" /><small>可输入站点入口或具体类目页；系统会记录来源 URL 并扫描类目。</small></label><div className="wizard-two-col"><label className="console-field"><span>任务名称（可选）</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label><label className="console-field"><span>目标说明（可选）</span><input value={goal} onChange={(event) => setGoal(event.target.value)} /></label></div><div className="wizard-footer"><p>点击后系统会完成：URL 预检 → 创建任务 → 扫描类目。</p><button type="button" className="console-button console-button--primary" onClick={() => void probeAndScan()} disabled={busy || scanning || !url.trim()}>{busy ? "摸底中…" : scanning ? "正在扫描类目…（后台）" : "预检并扫描类目 →"}</button></div></div></> : null}
      {step === 2 ? <><SectionTitle title="02 · 类目与选择" detail="一级类目为大类，点击「展开」查看二级类目和各自数量。可勾选一级（整个大类）、只勾二级（细分类目）或任意混合，勾选的都会进入工作流。" /><div className="qualification-card"><div><span className="console-card-kicker">预检结果</span><strong>{String(preflight?.domain ?? (() => { try { return new URL(url).hostname; } catch { return url; } })())}</strong><p>{String(preflight?.next_action ?? "可以开始类目发现")}</p></div><StatusBadge status={job?.status ?? "DRAFT"} /></div>{categories.some((category) => category.count_kind === "UNKNOWN") ? <div className="console-alert console-alert--attention"><strong>数量待补证</strong><span>当前类目链接已经发现，但没有可验证的商品总数。UNKNOWN 不代表 0；完成访问验证或重新摸底后，Website 会用同一会话继续 L2 数量补证。</span></div> : null}<div className="policy-summary-grid"><div><span>一级类目</span><strong>{topLevelCategories.length}</strong></div><div><span>子类目</span><strong>{categories.length - topLevelCategories.length}</strong></div><div><span>已勾选</span><strong>{selectedCategories.length}</strong></div><div><span>已勾选可见/估算合计</span><strong>{selectedCountLabel}</strong></div></div>{categories.length ? <div className="taxonomy-list">{topLevelCategories.map((root) => { const children = childCategoriesOf(root); const expanded = expandedGroups.includes(root.path); return <div className="taxonomy-group" key={root.category_id}><div className={`taxonomy-row taxonomy-row--parent ${selectedCategories.includes(root.category_id) ? "is-selected" : ""}`}><input type="checkbox" checked={selectedCategories.includes(root.category_id)} onChange={() => toggleCategory(root.category_id)} /><span><strong>{root.canonical_name}</strong><small>{root.native_name} · {root.path}{children.length ? ` · ${children.length} 个子类目` : ""}</small></span><b>{rootCountLabelOf(root)}</b>{children.length ? <button type="button" className="taxonomy-toggle" onClick={() => setExpandedGroups((current) => current.includes(root.path) ? current.filter((path) => path !== root.path) : [...current, root.path])}>{expanded ? "收起 ▴" : "展开 ▸"}</button> : null}</div>{expanded ? children.map((child) => <label className={`taxonomy-row taxonomy-row--child ${selectedCategories.includes(child.category_id) ? "is-selected" : ""}`} key={child.category_id}><input type="checkbox" checked={selectedCategories.includes(child.category_id)} onChange={() => toggleCategory(child.category_id)} /><span><strong>{child.canonical_name}</strong><small>{child.native_name} · {child.path}</small></span><b>{countLabelOf(child)}</b></label>) : null}</div>; })}<p className="taxonomy-group-hint">勾选一级 = 采集整个大类（含其全部子类目）；勾选二级 = 只采集该细分类目；支持任意混合勾选。</p></div> : <p className="console-empty-text">没有可验证类目；请查看扫描状态后重试或进入人工复核。</p>}<div className="wizard-footer"><button type="button" className="console-button console-button--quiet" onClick={() => setStep(1)}>← 返回</button><button type="button" className="console-button console-button--primary" onClick={() => setStep(3)} disabled={!categories.length}>下一步：设定目标 →</button></div></> : null}
      {step === 3 ? <><SectionTitle title="03 · 目标与策略" detail="你已经看到上面类目的数量，现在决定目标数量和边界。" /><div className="wizard-subsection"><div className="wizard-subsection-head"><h3>目标数量</h3><span>在已勾选 {selectedCategories.length} 个类目、约 {selectedCount} 件的基础上设定。</span></div><div className="segmented-control">{([["EXACT_N", "Exact N"], ["UP_TO_N", "Up To N"], ["ALL", "全部"]] as const).map(([value, text]) => <button type="button" key={value} className={targetMode === value ? "is-selected" : ""} onClick={() => setTargetMode(value)}>{text}</button>)}</div>{targetMode !== "ALL" ? <label className="console-field console-field--small"><span>{targetMode === "EXACT_N" ? "精确数量" : "最多数量"}</span><input type="number" min={1} max={5000} value={targetValue} onChange={(event) => setTargetValue(Math.max(1, Number(event.target.value) || 1))} /></label> : <p className="wizard-inline-note">“全部”会以站点类目扫描和 Registry 去重结果为边界。</p>}</div><div className="wizard-subsection"><div className="wizard-subsection-head"><h3>范围与默认安全策略</h3><span>这些选项会写入 Job Policy v1。</span></div><div className="wizard-three-col"><label className="console-field"><span>去重范围</span><select value={scope} onChange={(event) => setScope(event.target.value as typeof scope)}><option value="NEW_ONLY">只要新增产品</option><option value="TOTAL_INCLUDING_EXISTING">包含 Registry 已有产品</option></select></label><label className="console-field"><span>类目分配</span><select value={allocation} onChange={(event) => setAllocation(event.target.value as typeof allocation)}><option value="TOTAL_ACROSS_SELECTED">所选类目合计</option><option value="PER_CATEGORY">每个类目分别满足</option></select></label><label className="console-field"><span>目标不足时</span><select value={spillover} onChange={(event) => setSpillover(event.target.value as typeof spillover)}><option value="ASK">询问后再扩展</option><option value="STOP">直接停止</option><option value="AUTO_IF_EXPLICIT">仅用户明确允许时自动扩展</option></select></label><label className="console-field"><span>类目顺序</span><select value={strategy} onChange={(event) => setStrategy(event.target.value as typeof strategy)}><option value="SEQUENTIAL">按顺序补足</option><option value="EVEN">平均分配</option><option value="PROPORTIONAL">按 reported 比例</option><option value="CUSTOM">自定义配额</option></select></label></div></div><div className="wizard-footer"><button type="button" className="console-button console-button--quiet" onClick={() => setStep(2)}>← 返回</button><button type="button" className="console-button console-button--primary" onClick={() => void savePolicy()} disabled={busy || selectedCategories.length === 0}>{busy ? "保存中…" : "保存并进入生产计划 →"}</button></div></> : null}
      {step === 4 ? <><SectionTitle title="04 · 生产计划" detail="确认后点击「开始生产」，Website 会从所选类目自行发现产品；遇到访问挑战时保留同一浏览器会话并明确暂停。" /><div className="plan-head"><div><span className="console-card-kicker">Production Plan</span><h3>{job?.title}</h3><p>{job?.goal}</p></div><StatusBadge status={job?.status ?? "POLICY_READY"} /></div><div className="plan-grid"><div><span>预计任务量</span><strong>{targetMode === "ALL" ? "按扫描结果" : targetValue}</strong><small>不等于已交付</small></div><div><span>Provider</span><select className="console-input" value={provider} onChange={(event) => void saveProvider(event.target.value)} disabled={busy || !job}><option value="OFF">OFF（只形成 Ready Pool）</option><option value="lux3d">Lux3D 建模</option></select><small>{provider === "OFF" ? "不会产生付费调用，可先验证候选池" : "审批通过后按 Exact-N 调用"}</small></div><div><span>生产方式</span><strong>Website Engine</strong><small>Candidate Pool + workflow-event.v2</small></div><div><span>成本上限</span><strong>{provider === "OFF" ? "¥0.00" : "需显式审批"}</strong><small>{provider === "OFF" ? "Provider OFF" : "持久 ledger 防止重复提交"}</small></div></div><div className="plan-checklist"><p><span>✓</span> URL、站点和类目快照已写入审计记录</p><p><span>✓</span> Website 原生 L0/L1/L2 采集，不需要证据目录</p><p><span>✓</span> 目标不足默认询问，不自动跨类目</p></div><p className="wizard-inline-note">L2 浏览器会话由 Website 自动创建并按 Job 持久化；如果出现 CAPTCHA/验证，只需按提示处理后恢复同一 Job。</p>{askCeiling ? <div className="console-alert console-alert--attention"><strong>请输入本次最高建模成本</strong><p className="wizard-inline-note">内嵌环境不支持弹窗，请在此填写本次任务允许的最高建模成本（人民币元）。审批不会立即产生建模调用。</p><div className="wizard-input-row"><label className="console-field console-field--small"><span>成本上限（元）</span><input className="console-input" type="number" min={1} step={1} value={ceilingRaw} onChange={(event) => setCeilingRaw(event.target.value)} /></label><button type="button" className="console-button console-button--primary" onClick={() => void confirmPaywallCeiling()} disabled={busy}>{busy ? "审批并启动中…" : "确认成本上限并启动 →"}</button><button type="button" className="console-button console-button--quiet" onClick={() => setAskCeiling(false)} disabled={busy}>取消</button></div></div> : null}{run ? <><div className="plan-head"><div><span className="console-card-kicker">生产运行</span><h3>{run.stage} · {run.status}</h3><p>{run.items_total ? `${run.items_done} / ${run.items_total}` : "进度获取中…"}</p></div><StatusBadge status={run.status === "RUNNING" ? "RUNNING" : run.status === "SUCCEEDED" ? "COMPLETED" : "PRODUCTION_BLOCKED"} /></div><p className="console-empty-text">{run.progress_note ?? "等待 Production Engine 输出…"}</p>{run.workspace ? <p className="console-footnote">工作区：<code>{run.workspace}</code></p> : null}{run.stdout_tail ? <details className="developer-details"><summary>运行日志（末尾）</summary><pre>{run.stdout_tail}</pre></details> : null}{run.error ? <div className="console-alert console-alert--danger"><strong>失败原因</strong><span>{run.error}</span></div> : null}</> : null}<div className="wizard-footer"><button type="button" className="console-button console-button--quiet" onClick={() => setStep(3)}>← 返回</button><button type="button" className="console-button console-button--primary" onClick={() => void startProductionFlow()} disabled={busy || run?.status === "RUNNING"}>{busy ? "启动中…" : run?.status === "RUNNING" ? "生产中…" : "开始生产 →"}</button></div></> : null}
    </section>
    {job ? <p className="console-footnote">Job ID <code>{job.job_id}</code> · 创建于 {formatTime(job.created_at)} · <Link href={`/jobs/${encodeURIComponent(job.job_id)}`}>打开详情</Link></p> : null}
  </div>;
}

export function JobsPage() {
  const [jobs, setJobs] = useState<ControlJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editGoal, setEditGoal] = useState("");
  const [editTarget, setEditTarget] = useState(1);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  async function refresh() {
    try {
      const response = await getControlJobs();
      setJobs(response.items);
      setError(null);
    } catch (reason) { setError(readableConsoleError(reason)); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async read hydrates the persisted Job list.
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  function startEdit(job: ControlJob) {
    setEditingId(job.job_id);
    setEditTitle(job.title);
    setEditGoal(job.goal);
    setEditTarget(job.target_value ?? 1);
  }

  async function saveEdit(jobId: string) {
    try {
      await updateControlJob(jobId, { title: editTitle, goal: editGoal, target_value: editTarget });
      setEditingId(null); setActionMsg("已保存修改"); await refresh();
    } catch (reason) { setError(readableConsoleError(reason)); }
  }

  async function removeJob(job: ControlJob) {
    if (!window.confirm(`确定删除任务「${job.title}」？该操作不可撤销。`)) return;
    try {
      await deleteControlJob(job.job_id);
      setActionMsg(`已删除任务「${job.title}」`); await refresh();
    } catch (reason) { setError(readableConsoleError(reason)); }
  }

  const editingJob = editingId ? jobs.find((item) => item.job_id === editingId) ?? null : null;

  return <div className="console-page"><PageHeader eyebrow="任务 / 全部 Job" title="任务" description="查看生产任务与实时进度；支持编辑和删除。列表每 5 秒自动刷新。" action={<Link className="console-button console-button--primary" href="/jobs/new">＋ 新建任务</Link>} />{error ? <div className="console-alert console-alert--danger"><strong>读取失败</strong><span>{error}</span></div> : null}{actionMsg ? <div className="console-alert console-alert--success"><strong>已处理</strong><span>{actionMsg}</span></div> : null}<section className="console-panel"><SectionTitle title="全部任务" detail={busy ? "正在读取…" : `${jobs.length} 个 Job · 自动刷新`} />{jobs.length ? <div className="console-table-wrap"><table className="console-table"><thead><tr><th>任务</th><th>站点</th><th>目标</th><th>生产进度</th><th>阶段</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>{jobs.map((job) => { const run = job.run ?? null; const runLabel = !run ? "未开始" : run.status === "RUNNING" ? "● 生产中" : run.status === "SUCCEEDED" ? "完成" : run.status === "BLOCKED" ? "建模闸门" : run.status === "FAILED" ? "失败" : run.status; return <tr key={job.job_id}><td><strong>{job.title}</strong><small>{job.job_id}</small></td><td>{job.site_name}</td><td>{job.target_mode === "ALL" ? "全部" : job.target_value}<small>{job.counts.eligible_count} 合格 / {job.counts.delivered_count} 交付</small><div style={{ height: 6, borderRadius: 999, background: "var(--line)", overflow: "hidden", marginTop: 6, minWidth: 80 }}><div style={{ height: "100%", width: `${jobProgress(job)}%`, background: "var(--blue)", borderRadius: 999 }} /></div></td><td><span className={run?.status === "RUNNING" ? "console-row-action" : "drawer-muted"}>{runLabel}</span>{run && run.status === "RUNNING" ? <small> {run.stage}{run.items_total ? ` · ${run.items_done}/${run.items_total}` : ""}</small> : null}{run && run.status !== "RUNNING" && run.stage !== "launching" ? <small> {run.stage}</small> : null}</td><td>{label(job.current_stage)}</td><td><StatusBadge status={job.status} /></td><td>{formatTime(job.updated_at)}</td><td><Link className="console-row-action" href={`/jobs/${encodeURIComponent(job.job_id)}`}>打开</Link><button type="button" className="console-row-action" onClick={() => startEdit(job)}>编辑</button><button type="button" className="console-row-action" onClick={() => void removeJob(job)}>删除</button></td></tr>; })}</tbody></table></div> : <EmptyJobs />}{editingJob ? <div className="console-panel" style={{ marginTop: 16 }}><SectionTitle title={`编辑任务：${editingJob.title}`} detail="修改任务名称、目标说明或目标数量后保存。" /><div className="wizard-three-col"><label className="console-field"><span>任务名称</span><input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} /></label><label className="console-field"><span>目标说明</span><input value={editGoal} onChange={(event) => setEditGoal(event.target.value)} /></label><label className="console-field"><span>目标数量</span><input type="number" min={1} max={5000} value={editTarget} onChange={(event) => setEditTarget(Math.max(1, Number(event.target.value) || 1))} /></label></div><div className="wizard-footer"><button type="button" className="console-button console-button--quiet" onClick={() => setEditingId(null)}>取消</button><button type="button" className="console-button console-button--primary" onClick={() => void saveEdit(editingJob.job_id)}>保存修改</button></div></div> : null}</section></div>;
}

export function ReviewCenterPage() {
  const [reviews, setReviews] = useState<ControlReview[]>([]); const [error, setError] = useState<string | null>(null); const [actioned, setActioned] = useState<string | null>(null);
  async function load() { try { setReviews((await getControlReviews()).items); } catch (reason) { setError(readableConsoleError(reason)); } }
  useEffect(() => {
    let active = true;
    getControlReviews().then((response) => { if (active) setReviews(response.items); }).catch((reason) => { if (active) setError(readableConsoleError(reason)); });
    return () => { active = false; };
  }, []);
  async function resolve(reviewId: string, action: "ACCEPT" | "REQUEST_RESCAN" | "STOP") { try { await actOnReview(reviewId, { action, reason: "在 Review Center 完成处理", actor: "operator" }); setActioned(reviewId); await load(); } catch (reason) { setError(readableConsoleError(reason)); } }
  return <div className="console-page"><PageHeader eyebrow="Review Center / 人工闸门" title="Review Center" description="所有需要人工确认的事情集中到这里：访问阻断、目标不足、低置信度、证据异常和 Provider 安全。" action={<Link className="console-button console-button--quiet" href="/">返回 Dashboard</Link>} />{error ? <div className="console-alert console-alert--danger"><strong>读取失败</strong><span>{error}</span></div> : null}<div className="review-summary"><div><span>待处理</span><strong>{reviews.length}</strong></div><div><span>默认原则</span><strong>先证据，后动作</strong></div><div><span>付费操作</span><strong>必须二次确认</strong></div></div><section className="console-panel"><SectionTitle title="按人工动作分组" detail="每一项都说明影响范围、风险和下一步。" />{reviews.length ? <div className="review-center-list">{reviews.map((review) => <article className="review-center-item" key={review.review_id}><div className={`review-severity review-severity--${review.severity.toLowerCase()}`}>{review.severity}</div><div className="review-center-content"><div className="review-center-heading"><div><span className="console-eyebrow">{review.reason_code}</span><h3>{review.title}</h3></div><small>{formatTime(review.created_at)}</small></div><p>{review.detail}</p><div className="review-impact"><span>影响对象</span><b>{review.job_id ?? "系统级"}</b><span>付款/永久变更</span><b>{review.reason_code === "PROVIDER_SAFETY" ? "未发生" : "需确认"}</b></div><div className="review-actions"><button type="button" className="console-button console-button--primary" onClick={() => void resolve(review.review_id, "ACCEPT")} disabled={actioned === review.review_id}>确认并继续</button><button type="button" className="console-button console-button--secondary" onClick={() => void resolve(review.review_id, "REQUEST_RESCAN")} disabled={actioned === review.review_id}>请求重新取证</button><button type="button" className="console-button console-button--quiet" onClick={() => void resolve(review.review_id, "STOP")} disabled={actioned === review.review_id}>停止</button></div></div></article>)}</div> : <EmptyJobs />}</section></div>;
}

export function SystemStatusPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    void getSystemStatus()
      .then(setStatus)
      .catch((reason) => setError(readableConsoleError(reason)))
      .finally(() => setBusy(false));
  }, []);

  const diagnostics = status?.diagnostics;
  const l2 = status?.l2_browser ?? diagnostics?.l2_browser;
  const brain = diagnostics?.brain;
  const brainOverride = diagnostics?.brain_override;
  const lux3d = status?.lux3d ?? diagnostics?.lux3d;
  const blender = status?.blender ?? diagnostics?.blender;
  const productionWorker = status?.production_worker ?? diagnostics?.production_worker;
  const diagnosticValue = (value: string | undefined, fallback = busy ? "读取中…" : "未知") => value ?? fallback;
  const diagnosticTone = (value: string | undefined) => value === "READY" || value === "LOCAL_AGENT" || value === "ON" || value === "WEBSITE_NATIVE" ? "success" : value === "BUSY" || value === "RECEIPT_GATED" || value === "OFF_BY_DEFAULT" || value === "NOT_CONFIGURED" || value === "NOT_INSTALLED" ? "attention" : "danger";
  const workerRows = Object.fromEntries(Object.entries(status?.workers ?? { scrape: "读取中…", site_scan: "读取中…", modeling: "读取中…", qa: "读取中…" }).filter(([name]) => name !== "production"));

  return <div className="console-page">
    <PageHeader eyebrow="System / 运行边界" title="系统健康与安全" description="这里看 Website Native Runtime、实际生效的 Brain、L2、Blender、Provider Safety 和 Worker 状态。密钥、私有 URL、会话目录不会显示。" action={<Link className="console-button console-button--quiet" href="/">返回 Dashboard</Link>} />
    {error ? <div className="console-alert console-alert--danger"><strong>系统状态读取失败</strong><span>{error}</span></div> : null}
    {brainOverride?.status === "ON" || status?.website_brain.local_agent_override ? <div className="console-alert console-alert--attention"><strong>Local Agent Override 已生效</strong><span>{brainOverride?.reason ?? status?.website_brain.override_reason ?? "当前复核由本地 Agent 完成，不发起外部 Brain 请求。"}</span></div> : null}
    <div className="system-health-grid">
      <HealthStatusCard title="Website API" value={diagnosticValue(diagnostics?.api?.status, error ? "ERROR" : "READY")} detail="控制平面响应正常" tone={error ? "danger" : "success"} />
      <HealthStatusCard title="Database" value={diagnosticValue(diagnostics?.database?.status ?? status?.database.status)} detail="状态来自实际 SELECT 1 检查" tone={diagnosticTone(diagnostics?.database?.status ?? status?.database.status)} />
      <HealthStatusCard title="L2 Browser" value={diagnosticValue(l2?.status)} detail={`${l2?.engine ?? "chromium"} · ${l2?.mode ?? "ISOLATED_PERSISTENT"}`} tone={diagnosticTone(l2?.status)} />
      <HealthStatusCard title="Website Brain" value={diagnosticValue(brain?.status ?? (status?.website_brain.review_provider === "LOCAL_AGENT" ? "LOCAL_AGENT" : status?.website_brain.status))} detail={brain?.effective_mode === "LOCAL_AGENT" || status?.website_brain.review_provider === "LOCAL_AGENT" ? "本地 Agent · 不发外部 API" : status?.website_brain.configured ? "已配置独立 Brain" : "未配置 WEBSITE_BRAIN_*"} tone={diagnosticTone(brain?.status ?? status?.website_brain.status)} />
    </div>
    <div className="system-grid">
      <section className="console-panel">
        <SectionTitle title="实际运行边界" detail="所有状态都来自当前进程、运行时探测或持久化数据库；不因配置模板而伪造 READY。" />
        <div className="system-kv">
          <div><span>Brain Effective Mode</span><strong>{diagnosticValue(brain?.effective_mode ?? status?.website_brain.model_mode)}</strong></div>
          <div><span>Brain Override</span><strong>{diagnosticValue(brainOverride?.status, "OFF")}</strong></div>
          <div><span>Lux3D</span><strong>{diagnosticValue(lux3d?.status)}</strong></div>
          <div><span>Blender</span><strong>{diagnosticValue(blender?.status)}</strong></div>
          <div><span>Provider Safety</span><strong>{diagnosticValue(status?.provider.status)}</strong></div>
          <div><span>Object Storage</span><strong>{diagnosticValue(status?.object_storage.status)}</strong></div>
          <div><span>Brain namespace</span><strong>{status?.website_brain.namespace ?? "WEBSITE_BRAIN_*"}</strong></div>
          <div><span>Brain posts</span><strong>{String(status?.website_brain.provider_posts ?? 0)}</strong></div>
        </div>
        <details className="developer-details"><summary>开发诊断</summary><pre>{JSON.stringify({ diagnostics, brain: status?.website_brain, provider: status?.provider }, null, 2)}</pre></details>
      </section>
      <section className="console-panel">
        <SectionTitle title="Worker 与队列" detail="生产槽位和扫描队列取自数据库；单个站点阻断不会伪装成全局失败。" />
        <div className="health-rows">
          {Object.entries(workerRows).map(([name, value]) => <HealthRow key={name} label={`${name} worker`} value={value} tone={diagnosticTone(value)} />)}
          <HealthRow label="production worker" value={productionWorker?.status ?? "读取中…"} tone={diagnosticTone(productionWorker?.status)} />
        </div>
      </section>
    </div>
  </div>;
}

function HealthStatusCard({ title, value, detail, tone: cardTone }: { title: string; value: string; detail: string; tone: string }) { return <section className="health-status-card"><span className={`signal-dot signal-dot--${cardTone}`} /><div><span>{title}</span><strong>{value}</strong><small>{detail}</small></div></section>; }

export function PlaceholderConsolePage({ section }: { section: "sites" | "registry" | "delivery" }) {
  const copy = section === "sites" ? { eyebrow: "Sites / Site Registry", title: "站点与类目", description: "查看站点 Profile、健康状态、原生类目快照和 Profile Drift。规则由 Website Native Site Analyzer 维护。", title2: "Site Registry" } : section === "registry" ? { eyebrow: "Products / Registry", title: "产品 Registry", description: "永久去重、来源证据、命名、尺寸、建模与交付状态汇总。没有证据的产品不会进入 Ready Pool。", title2: "等待真实产品记录" } : { eyebrow: "Delivery / QA", title: "交付", description: "GLB QA、批次清单、文件哈希和交付状态在这里统一核对。部分成功会显式保留缺失项。", title2: "等待交付批次" };
  return <div className="console-page"><PageHeader eyebrow={copy.eyebrow} title={copy.title} description={copy.description} action={<Link href="/" className="console-button console-button--quiet">返回 Dashboard</Link>} /><section className="console-panel console-panel--empty-page"><div className="console-empty-mark">{section === "sites" ? "⌂" : section === "registry" ? "◈" : "⇩"}</div><h2>{copy.title2}</h2><p>当前数据库还没有真实记录。创建任务并完成 Website Native 的公开证据门后，这里会显示可审计对象，而不是示例数据。</p>{section === "sites" ? <Link href="/jobs/new" className="console-button console-button--secondary">从新任务开始</Link> : <Link href="/jobs" className="console-button console-button--secondary">查看任务</Link>}</section></div>;
}


function jobProgress(job: ControlJob): number {
  const target = job.target_mode === "ALL" ? job.counts.eligible_count : (job.target_value ?? 0);
  if (target <= 0) return 0;
  return Math.min(100, Math.round((job.counts.delivered_count / target) * 100));
}

export function SitesPage() {
  const [sites, setSites] = useState<ControlSite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [addMsg, setAddMsg] = useState<string | null>(null);
  const [rescanKey, setRescanKey] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const scanTimer = useRef<number | null>(null);
  // 轮询代数：每次新发摸底 +1，旧轮询链检测到代数过期即自弃，
  // 防止「旧扫描的结果覆盖新扫描的通知」。
  const pollGen = useRef(0);
  const trackedScanId = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      if (scanTimer.current !== null) { window.clearTimeout(scanTimer.current); scanTimer.current = null; }
      trackedScanId.current = null;
    };
  }, []);
  function handleUrlChange(value: string) {
    setUrl(value);
  }

  async function refreshSiteCards() {
    try {
      const response = await getControlSites();
      setSites(response.items);
    } catch { /* 轮询结果已落库；下一轮或手动刷新会重试列表读取 */ }
  }

  // 轮询摸底结果：只认「本轮发起的那一次扫描」（按 scan_id 精确匹配），
  // 不再靠时间窗口过滤，避免把更早的旧扫描（含曾经的失败/未取得类目）误当成本轮结果反复提示。
  // 进行中实时给出阶段提示（含 L2 浏览器人工接管），到达终态后结束并刷新类目。
  async function pollSiteScan(siteKey: string, scanId: string, attempt: number, gen: number) {
    if (gen !== pollGen.current) return; // 已有更新的摸底，放弃本链
    if (attempt >= 120) {
      if (gen === pollGen.current) {
        if (trackedScanId.current === scanId) trackedScanId.current = null;
        setRescanKey(null);
        setAddMsg(`${siteKey} 摸底超过约 6 分钟未完成，请稍后点「刷新」查看持久化结果。`);
      }
      return;
    }
    try {
      const detail = await getControlSite(siteKey);
      const scans = Array.isArray(detail.scans) ? detail.scans : [];
      const scan = scans.find((item) => String((item as { scan_id?: unknown })?.scan_id ?? "") === scanId)
        ?? (attempt < 3 ? scans[0] : undefined); // 前几轮列表可能尚未包含新扫描，用最新一条兜底
      if (gen !== pollGen.current) return;
      if (scan) {
        const status = String((scan as { status?: unknown })?.status ?? "");
        const finished = Boolean((scan as { finished_at?: unknown })?.finished_at);
        if (status === "READY" || status === "PARTIAL") {
          const count = detail.categories?.length ?? 0;
          if (trackedScanId.current === scanId) trackedScanId.current = null;
          setRescanKey(null);
          setAddMsg(status === "READY"
            ? `${siteKey} 摸底完成，最新 ${count} 个类目`
            : `${siteKey} 摸底完成（${count} 个类目，部分证据待复核）`);
          await refreshSiteCards();
          return;
        }
        if (finished && ["HUMAN_REQUIRED", "ROBOTS_DENIED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "FAILED", "BROWSER_RUNTIME_NOT_INSTALLED", "BRAIN_NOT_CONFIGURED"].includes(status)) {
          if (trackedScanId.current === scanId) trackedScanId.current = null;
          setRescanKey(null);
          const errorMessage = String((scan as { error_message?: unknown })?.error_message ?? "");
          const errorCode = String((scan as { error_code?: unknown })?.error_code ?? "");
          const why = errorMessage ? `（${errorCode || "BLOCKED"}：${errorMessage}）` : "";
          setAddMsg(`${siteKey} 摸底未取得类目${why}，已保留原有数据`);
          await refreshSiteCards();
          return;
        }
        // 仍在进行：实时阶段提示（含 L2 浏览器人工接管提示）
        const stageHint = status === "L2_BROWSER"
          ? "：可见浏览器已打开；系统会先自行判断页面，只有出现明确验证控件才需要人工"
          : status === "ANALYZING" ? "：正在解析类目与数量" : "";
        setAddMsg(`${siteKey} 摸底中（${status || "QUEUED"}${stageHint}）`);
      }
    } catch { /* 轮询失败则下轮重试 */ }
    scanTimer.current = window.setTimeout(() => { void pollSiteScan(siteKey, scanId, attempt + 1, gen); }, 3000);
  }

  function trackScan(siteKey: string, scanId: string) {
    if (!scanId || trackedScanId.current === scanId) return;
    if (scanTimer.current !== null) { window.clearTimeout(scanTimer.current); scanTimer.current = null; }
    trackedScanId.current = scanId;
    setRescanKey(siteKey);
    pollGen.current += 1;
    void pollSiteScan(siteKey, scanId, 0, pollGen.current);
  }

  async function resumeSiteScanFromCard(site: ControlSite) {
    if (rescanKey || !site.latest_scan_id) return;
    setRescanKey(site.site_key); setAddMsg(null); setError(null);
    try {
      const scan = await resumeSiteScan(site.latest_scan_id);
      setAddMsg(`${site.display_name} 已恢复同一浏览器会话，继续扫描 ${scan.scan_id}。`);
      await refreshSiteCards();
      trackScan(site.site_key, scan.scan_id);
    } catch (reason) {
      setError(readableConsoleError(reason));
      setRescanKey(null);
    }
  }

  async function refresh() {
    setBusy(true); setError(null);
    try {
      const response = await getControlSites();
      setSites(response.items);
      // 首次打开/刷新时，如果扫描已经在后台进行，自动接上该 scan_id。
      // 这样不会把“扫描尚未落库”的瞬时空列表误显示成最终结果。
      const pending = response.items.find((site) =>
        !!site.latest_scan_id && ACTIVE_SITE_SCAN_STATUSES.has(String(site.latest_scan_status ?? "")),
      );
      if (pending?.latest_scan_id) trackScan(pending.site_key, pending.latest_scan_id);
    } catch (reason) { setError(readableConsoleError(reason)); } finally { setBusy(false); }
  }
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async read hydrates the persisted Site list.
    void refresh();
    // refresh is intentionally the one-time mount loader; later refreshes are event-driven.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function addSite() {
    const clean = url.trim();
    if (!clean || adding) return;
    if (!/^https?:\/\/.+/i.test(clean)) { setError("网址格式不对，请输入类似 https://example.com 的完整网址"); return; }
    setAdding(true); setAddMsg(null); setError(null);
    try {
      let host = clean;
      try { host = new URL(clean).hostname; } catch { /* 保留原始输入 */ }
      const check = await preflightSite(clean, true);
      if (check.status === "FAILED" || check.status === "HUMAN_REQUIRED") {
        const blockerInfo = (check as { blocker?: { code?: string; message?: string } }).blocker;
        const isTimeout = /timeout|timed out/i.test(String(blockerInfo?.message ?? "")) || String(blockerInfo?.code ?? "").includes("TIMEOUT");
        throw new Error(isTimeout
          ? "该站点当前无法访问（连接超时）。请检查本机网络或开启代理后重试。"
          : `该站点当前无法访问（${String(blockerInfo?.code ?? check.status)}）。请确认网址正确且站点公开可访问。`);
      }
      const domain = String((check as Record<string, unknown>)?.domain ?? host);
      const siteKey = domain.replace(/^www\./i, "");
      const scan = await scanSite(clean, true);
      setUrl("");
      setAddMsg(`${domain} 的扫描 ${scan.scan_id} 已进入持久化队列；可离开本页，结果会保存在网站档案。`);
      await refresh();
      trackScan(siteKey, scan.scan_id);
    } catch (reason) { setError(readableConsoleError(reason)); } finally { setAdding(false); }
  }

  // 重新摸底（更新）：对已有站点再跑一次完整扫描，成功后类目以最新快照为准。
  async function rescanSite(site: ControlSite) {
    if (rescanKey) return;
    const target = site.source_url || `https://${site.domain}`;
    setRescanKey(site.site_key); setAddMsg(null); setError(null);
    try {
      const scan = await scanSite(target, true);
      setAddMsg(`${site.display_name} 的重新扫描 ${scan.scan_id} 已排队；旧快照在新结果完成前继续保留。`);
      trackScan(site.site_key, scan.scan_id);
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally { /* 由持久化扫描终态清理按钮状态 */ }
  }

  async function removeSite(site: ControlSite) {
    if (deletingKey) return;
    const confirmed = window.confirm(`确定删除网站「${site.display_name}」吗？\n\n将删除：类目、快照、扫描历史和入口 URL。\n${site.job_count ? `该网站有 ${site.job_count} 个历史任务，任务记录会保留，可到任务列表单独删除。` : ""}`);
    if (!confirmed) return;
    setDeletingKey(site.site_key); setError(null);
    try {
      await deleteControlSite(site.site_key);
      setAddMsg(`已删除 ${site.display_name}`);
      await refresh();
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally { setDeletingKey(null); }
  }

  return <div className="console-page">
    <PageHeader eyebrow="网站库 / 已导入站点" title="网站库" description="这里是你导入过的所有网站档案。点「查看类目」看每个类目和数量；要发起爬取，统一去「新建任务」。" action={<button className="console-button console-button--quiet" type="button" onClick={() => void refresh()} disabled={busy}>{busy ? "读取中…" : "刷新"}</button>} />
    {error ? <div className="console-alert console-alert--danger"><strong>读取失败</strong><span>{error}</span><button type="button" onClick={() => void refresh()}>重试</button></div> : null}
    {addMsg ? <div className={`console-alert ${/未取得|超过|失败/.test(addMsg) ? "console-alert--attention" : "console-alert--success"}`}><strong>{/未取得|超过|失败/.test(addMsg) ? "提示" : "操作结果"}</strong><span>{addMsg}</span></div> : null}
    <section className="console-panel">
      <SectionTitle title="添加网站（自动摸底）" detail="输入任意站点 URL，系统先做预检并扫描类目，把网站收进档案库。" />
      <div className="sites-add-bar">
        <input className="console-input" value={url} onChange={(event) => handleUrlChange(event.target.value)} placeholder="https://example-shop.com" />
        <button className="console-button console-button--primary" type="button" onClick={() => void addSite()} disabled={adding || !url.trim()}>{adding ? "摸底中…" : "添加并摸底"}</button>
      </div>
    </section>
    {sites.length ? <div className="sites-grid">{sites.map((site) => {
      const segs = (path: string) => path.split("/").filter(Boolean).length;
      const catLevel = (category: { level?: number; path: string }) => category.level ?? (segs(category.path) >= 2 ? 2 : 1);
       const scanStatus = String(site.latest_scan_status ?? "");
       const scanPending = ACTIVE_SITE_SCAN_STATUSES.has(scanStatus);
       const taxonomyAvailable = site.taxonomy_available !== false && !scanPending;
       const countL1 = taxonomyAvailable ? site.categories.filter((category) => catLevel(category) === 1).length : "—";
       const countL2 = taxonomyAvailable ? site.categories.filter((category) => catLevel(category) === 2).length : "—";
      const scanCanResume = Boolean(site.latest_scan_id && ["HUMAN_REQUIRED", "TEMPORARY_FAILURE", "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "FAILED", "BROWSER_RUNTIME_NOT_INSTALLED", "PARTIAL", "BRAIN_NOT_CONFIGURED"].includes(scanStatus));
      const launchHref = `/jobs/new?url=${encodeURIComponent(site.source_url || `https://${site.domain}`)}`;
      return <section className="console-panel site-card" key={site.site_key}>
        <div className="site-card-head">
          <div className="site-card-title"><div className="simple-brand-mark site-favicon" /><div><h3>{site.display_name}</h3><p>{site.domain}</p></div></div>
          <StatusBadge status={site.status} />
        </div>
        <div className="site-card-stats">
          <div><span>一级类目</span><strong>{countL1}</strong></div>
          <div><span>二级类目</span><strong>{countL2}</strong></div>
          <div><span>任务</span><strong>{site.job_count}</strong></div>
          <div><span>已交付</span><strong>{site.delivered_count}</strong></div>
        </div>
         {scanPending ? <p className="site-card-scan-state"><i className="signal-dot signal-dot--attention" />类目扫描中（{scanStatus}），完成后自动刷新；当前数量不代表 0</p> : null}
         {!scanPending && !taxonomyAvailable ? <p className="site-card-scan-state"><i className="signal-dot signal-dot--attention" />当前类目证据不可用（{label(site.taxonomy_state ?? scanStatus, "UNKNOWN")}），数量未知</p> : null}
        {scanCanResume && !scanPending ? <p className="site-card-scan-state"><i className="signal-dot signal-dot--attention" />最新扫描为 {label(scanStatus)}，当前不把旧类目当作已验证结果；可恢复同一扫描补全证据和数量</p> : null}
        <div className="site-card-actions">
          <Link className="console-button console-button--secondary" href={`/sites/${encodeURIComponent(site.site_key)}`}>查看档案 →</Link>
          {scanCanResume ? <button className="console-button console-button--secondary" type="button" onClick={() => void resumeSiteScanFromCard(site)} disabled={rescanKey === site.site_key} title="保留同一可见浏览器会话，继续处理访问挑战或补全证据">{rescanKey === site.site_key ? "恢复中…" : "恢复同一扫描"}</button> : null}
          <button className="console-button console-button--secondary" type="button" onClick={() => void rescanSite(site)} disabled={rescanKey === site.site_key} title="重新扫描该网站，类目与数量以最新结果为准">{rescanKey === site.site_key ? "摸底中…" : "重新摸底"}</button>
          <button className="console-button console-button--danger" type="button" onClick={() => void removeSite(site)} disabled={deletingKey === site.site_key} title="删除该网站档案（类目、快照、扫描历史）">{deletingKey === site.site_key ? "删除中…" : "删除"}</button>
          <Link className="console-button console-button--primary" href={launchHref}>开始任务 →</Link>
        </div>

      </section>;
    })}</div> : <div className="console-empty"><div className="console-empty-mark">⌂</div><div><strong>还没有导入任何网站</strong><p>在上方输入一个站点 URL，系统会先摸底类目和数量，把网站收进档案库。</p></div></div>}
  </div>;
}

export function SiteDetailPage({ siteKey }: { siteKey: string }) {
  const [detail, setDetail] = useState<ControlSiteDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    getControlSite(siteKey).then((payload) => { if (active) setDetail(payload); }).catch((reason) => { if (active) setError(readableConsoleError(reason)); });
    return () => { active = false; };
  }, [siteKey]);
  const site = detail?.site ?? {};
  return <div className="console-page">
    <div className="console-breadcrumb"><Link href="/sites">网站</Link><span>/</span><span>{String(site.display_name ?? siteKey)}</span></div>
    <PageHeader eyebrow="网站档案 / Site Detail" title={String(site.display_name ?? siteKey)} description="查看入口 URL、类目快照、数量证据、扫描历史和关联任务。网站扫描与生产 Job 的生命周期彼此独立。" action={<Link className="console-button console-button--primary" href={`/jobs/new?url=${encodeURIComponent(String(detail?.entry_urls?.[0]?.url ?? `https://${siteKey}`))}`}>按此网站建任务 →</Link>} />
    {error ? <div className="console-alert console-alert--danger"><strong>档案读取失败</strong><span>{error}</span></div> : null}
    {!detail ? <div className="console-panel console-loading">读取站点档案…</div> : <>
      <div className="job-detail-summary"><div><span>状态</span><strong><StatusBadge status={String(site.status ?? "UNKNOWN")} /></strong><small>{String(site.domain ?? siteKey)}</small></div><div><span>类目</span><strong>{detail.taxonomy_available === false ? "未知" : detail.categories.length}</strong><small>{label(detail.taxonomy_state, "当前快照")}</small></div><div><span>数量</span><strong>{detail.count_state === "UNKNOWN" ? "未知" : detail.reported_total ?? "—"}</strong><small>{detail.count_state ?? "UNKNOWN"}</small></div><div><span>任务</span><strong>{detail.jobs.length}</strong><small>关联 Job</small></div></div>
      <div className="job-detail-grid"><section className="console-panel"><SectionTitle title="Taxonomy" detail="数量值严格区分 EXACT、ESTIMATED、UNKNOWN。" />{detail.categories.length ? <div className="taxonomy-list">{detail.categories.map((category) => <div className="taxonomy-row" key={category.category_id}><span><strong>{category.canonical_name}</strong><small>{category.native_name} · {category.path}</small></span><b>{taxonomyCountLabel(category.count_kind, category.count_value)}</b></div>)}</div> : <p className="console-empty-text">当前没有可用的最新类目；历史快照仍保留在扫描记录中。</p>}</section><section className="console-panel"><SectionTitle title="Scan History" detail="每次扫描都保留状态、Brain 状态和 receipt 路径。" />{detail.scans.length ? <div className="event-timeline">{detail.scans.map((scan) => <div key={String(scan.scan_id)}><span>↗</span><div><strong>{String(scan.status)} · {String(scan.taxonomy_level)}</strong><small>{String(scan.source_url)} · Brain {String(scan.brain_status)} · POST {String(scan.provider_posts)} · {formatTime(String(scan.started_at))}</small>{scan.error_message ? <p className="drawer-muted">{String(scan.error_message)}</p> : null}</div></div>)}</div> : <p className="console-empty-text">还没有扫描记录。</p>}</section></div>
      <section className="console-panel" style={{ marginTop: 16 }}><SectionTitle title="Entry URLs & Jobs" detail="输入过的 URL 和后续任务会持续保留在站点档案中。" /><div className="system-kv">{detail.entry_urls.map((entry) => <div key={String(entry.url)}><span>入口 URL</span><strong>{String(entry.url)}</strong><small>{String(entry.last_status)} · 最近 {formatTime(String(entry.last_seen_at))}</small></div>)}{detail.jobs.map((job) => <div key={job.job_id}><span>Job</span><strong><Link href={`/jobs/${encodeURIComponent(job.job_id)}`}>{job.title}</Link></strong><small>{label(job.status)} · {formatTime(job.updated_at)}</small></div>)}</div></section>
    </>}
  </div>;
}

export function DeliveryPage() {
  const [deliveries, setDeliveries] = useState<ControlDelivery[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    let active = true;
    getControlDeliveries().then((response) => { if (active) setDeliveries(response.items); }).catch((reason) => { if (active) setError(readableConsoleError(reason)); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);
  const downloadUrl = (delivery: ControlDelivery, batch: { artifact_id?: string; name: string; file_count: number; download_path: string }) => batch.artifact_id
    ? `${API_ROOT}/control/deliveries/artifacts/${encodeURIComponent(batch.artifact_id)}/download`
    : `${API_ROOT}/control/deliveries/download?batch_path=${encodeURIComponent(`${delivery.relative_path}/${batch.name}`)}`;
  return <div className="console-page">
    <PageHeader eyebrow="交付下载 / 成果" title="交付下载" description="按批次浏览已完成的模型交付，每批 20 个模型打包成一个 zip 下载；图片集同样按类目归档。" />
    {busy ? <p className="console-empty-text">正在读取交付收据…</p> : null}{error ? <div className="console-alert console-alert--danger"><strong>读取失败</strong><span>{error}</span></div> : null}
    {deliveries.length ? <div className="delivery-grid">{deliveries.map((delivery) => <section className="console-panel delivery-card" key={delivery.delivery_id}>
      <div className="delivery-card-head"><div><h3>{delivery.delivery_id}</h3><p>{delivery.relative_path}</p></div><StatusBadge status="COMPLETED" /></div>
      <div className="site-card-stats"><div><span>批次</span><strong>{delivery.batch_count}</strong></div><div><span>模型总数</span><strong>{delivery.model_count}</strong></div><div><span>更新时间</span><strong>{formatTime(delivery.modified_at)}</strong></div></div>
      <div className="delivery-batches">{delivery.batches.map((batch) => <div className="delivery-batch" key={batch.name}><div><b>{batch.name}</b><small>{batch.file_count} 个模型</small></div><a className="console-button console-button--secondary" href={downloadUrl(delivery, batch)}>下载 zip</a></div>)}</div>
    </section>)}</div> : <div className="console-empty"><div className="console-empty-mark">⇩</div><div><strong>还没有交付批次</strong><p>完成爬虫与建模后，交付会按每 20 个模型一组出现在这里，可打包下载。</p></div></div>}
  </div>;
}
