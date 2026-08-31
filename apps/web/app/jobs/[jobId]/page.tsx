"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { API_ROOT } from "@/lib/api";
import {
  approveControlJob,
  getControlJob,
  getLocalReviewCandidate,
  readableConsoleError,
  recordLocalAgentReview,
  resumeSiteScan,
  resumeProduction,
  startProduction,
  type ControlArtifact,
  type ControlCategory,
  type ControlJob,
  type LocalReviewCandidate,
  type ProductionRun,
  type ProviderLedgerItem,
  type SiteScanRunResult,
} from "@/lib/console-api";

const STATUS: Record<string, string> = {
  DRAFT: "草稿",
  QUEUED: "排队中",
  SITE_SCAN_QUEUED: "扫描已排队",
  WAITING_REVIEW: "等待复核",
  TAXONOMY_READY: "类目已就绪",
  POLICY_READY: "策略待确认",
  PRODUCTION_BLOCKED: "生产已阻断",
  APPROVAL_RECORDED: "审批已记录",
  PRODUCTION_READY: "生产已放行",
  READY_POOL: "Ready Pool 已保存",
  BLOCKED: "流程已暂停",
  FAILED: "失败",
  TARGET_SHORTAGE: "目标不足",
  BRAIN_NOT_CONFIGURED: "Brain 未配置",
  COMPLETED: "已交付",
  RUNNING: "运行中",
  HUMAN_REQUIRED: "需要人工",
};
const STAGES = [
  { key: "SITE_SCAN", label: "站点扫描", detail: "Source Type、类目、数量与范围" },
  { key: "TARGET_POLICY", label: "目标策略", detail: "选定类目、Exact-N 与边界" },
  { key: "DISCOVERY", label: "产品发现", detail: "范围内发现、身份与 Registry 去重" },
  { key: "MEDIA", label: "媒体与视觉复核", detail: "主图绑定、Local Agent / Vision、类目与日期" },
  { key: "DIMENSION", label: "尺寸与命名", detail: "尺寸证据、Source Policy 与 50 字符限制" },
  { key: "READY_POOL", label: "Ready Pool", detail: "Catalog/Model Input Lock" },
  { key: "PROVIDER", label: "Provider", detail: "Safety、幂等提交、轮询与恢复" },
  { key: "DELIVERY", label: "GLB QA 与交付", detail: "Exact-N 校验与下载收据" },
];

type LocalReviewForm = {
  eligible: boolean;
  singleProduct: boolean;
  backgroundOk: boolean;
  imageTo3dSuitable: boolean;
  categoryGroup: string;
  productType: string;
  style: string;
  color: string;
  material: string;
  feature: string;
  width: string;
  depth: string;
  height: string;
  dimensionUnit: string;
  confidence: string;
};

const EMPTY_LOCAL_REVIEW_FORM: LocalReviewForm = {
  eligible: false,
  singleProduct: false,
  backgroundOk: false,
  imageTo3dSuitable: false,
  categoryGroup: "",
  productType: "",
  style: "",
  color: "",
  material: "",
  feature: "",
  width: "",
  depth: "",
  height: "",
  dimensionUnit: "",
  confidence: "0.00",
};

function stageIndex(value: string) {
  const stage = value.toUpperCase();
  if (/DELIVERY|COMPLETED/.test(stage)) return 7;
  if (/PROVIDER|MODELING|RAW_GLB|RECONCILIATION/.test(stage)) return 6;
  if (/READY_POOL|CATALOG|MODEL_INPUT/.test(stage)) return 5;
  if (/DIMENSION|NAMING/.test(stage)) return 4;
  if (/MEDIA|VISUAL|BRAIN|CATEGORY|DATE|CAPTURE/.test(stage)) return 3;
  if (/DISCOVERY|ACQUISITION|L2_BROWSER/.test(stage)) return 2;
  if (/TARGET|POLICY|PRODUCTION_PLAN/.test(stage)) return 1;
  return 0;
}

export default function JobDetailRoute({ params }: { params: Promise<{ jobId: string }> }) {
  const [job, setJob] = useState<ControlJob | null>(null);
  const [categories, setCategories] = useState<ControlCategory[]>([]);
  const [artifacts, setArtifacts] = useState<ControlArtifact[]>([]);
  const [events, setEvents] = useState<Array<Record<string, unknown>>>([]);
  const [run, setRun] = useState<ProductionRun | null>(null);
  const [siteScan, setSiteScan] = useState<SiteScanRunResult | null>(null);
  const [candidatePool, setCandidatePool] = useState<Record<string, unknown>>({});
  const [providerTasks, setProviderTasks] = useState<ProviderLedgerItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  // 内嵌环境不支持 window.prompt()，Provider 付费审批的成本上限改用应用内输入行采集。
  const [askCeiling, setAskCeiling] = useState(false);
  const [ceilingRaw, setCeilingRaw] = useState("100");
  const [localReviewCandidate, setLocalReviewCandidate] = useState<LocalReviewCandidate | null>(null);
  const [localReviewForm, setLocalReviewForm] = useState<LocalReviewForm>(EMPTY_LOCAL_REVIEW_FORM);
  const [localReviewBusy, setLocalReviewBusy] = useState(false);

  const load = useCallback(async () => {
    const { jobId } = await params;
    const payload = await getControlJob(jobId);
    setJob(payload.job);
    setCategories(payload.categories);
    setArtifacts(payload.artifacts);
    setEvents(payload.events);
    setRun(payload.run);
    setSiteScan(payload.site_scan);
    setCandidatePool(payload.candidate_pool);
    setProviderTasks(payload.provider_tasks);
  }, [params]);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        await load();
      } catch (reason) {
        if (active) setError(readableConsoleError(reason));
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [load]);

  async function startRunNow() {
    if (!job) return;
    setActionMsg(null);
    if (job.provider !== "OFF" && job.provider_safety !== "PRODUCTION_READY") {
      setAskCeiling(true);
      return;
    }
    await commitStart(job.job_id);
  }

  async function confirmCeiling() {
    if (!job) return;
    setStarting(true);
    setActionMsg(null);
    try {
      const ceilingMinor = Math.round(Number(ceilingRaw) * 100);
      if (!Number.isFinite(ceilingMinor) || ceilingMinor <= 0) throw new Error("成本上限必须大于 0");
      const approval = await approveControlJob(job.job_id, { confirm: true, approved_cost_ceiling_minor: ceilingMinor, actor: "website-operator" });
      setJob(approval.job);
      if (approval.status !== "PRODUCTION_READY") throw new Error(approval.job.last_reason ?? "Provider qualification 未通过");
      setAskCeiling(false);
      await commitStart(job.job_id);
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally {
      setStarting(false);
    }
  }

  async function commitStart(jobId: string) {
    setStarting(true);
    setActionMsg(null);
    try {
      const result = await startProduction(jobId);
      setActionMsg(result.started ? "生产已启动，正在后台执行…" : result.reason === "ALREADY_RUNNING" ? "该任务已有生产在运行。" : result.reason === "RESUME_REQUIRED" ? "该任务已安全暂停，请使用“恢复同一 Job”。" : "生产未启动，请稍后重试。");
      await load();
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally {
      setStarting(false);
    }
  }

  async function resumeRunNow() {
    if (!job) return;
    setStarting(true);
    setActionMsg(null);
    try {
      const result = await resumeProduction(job.job_id);
      setActionMsg(result.started ? "同一 Job 已恢复，正在继续读取 Native Runtime 事件。" : result.reason === "ALREADY_RUNNING" ? "该 Job 已在运行。" : "恢复请求已进入持久化队列。 ");
      await load();
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally {
      setStarting(false);
    }
  }

  async function resumeScanNow() {
    if (!siteScan) return;
    setStarting(true);
    setActionMsg(null);
    try {
      const result = await resumeSiteScan(siteScan.scan_id);
      setActionMsg(`站点扫描 ${result.scan_id} 已从同一浏览器会话恢复。`);
      await load();
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally {
      setStarting(false);
    }
  }

  async function openLocalReview(candidateId: string) {
    if (!job) return;
    setError(null);
    setActionMsg(null);
    setLocalReviewBusy(true);
    try {
      const payload = await getLocalReviewCandidate(job.job_id, candidateId);
      if (!payload.local_agent_enabled) throw new Error("当前未启用 LOCAL_AGENT；请先配置 Website Brain 或 LOCAL_REVIEW_MODE=agent。");
      const candidate = payload.candidate;
      const existing = candidate.local_agent_review ?? {};
      const dimensions = candidate.source_dimensions ?? {};
      setLocalReviewCandidate(candidate);
      setLocalReviewForm({
        eligible: Boolean(existing.eligible ?? false),
        singleProduct: Boolean(existing.single_product ?? false),
        backgroundOk: Boolean(existing.background_ok ?? false),
        imageTo3dSuitable: Boolean(existing.image_to_3d_suitable ?? false),
        categoryGroup: String(existing.category_group ?? candidate.category_group ?? ""),
        productType: String(existing.product_type ?? ""),
        style: String(existing.style ?? ""),
        color: String(existing.color ?? ""),
        material: String(existing.material ?? ""),
        feature: String(existing.feature ?? ""),
        width: String(existing.width ?? dimensions.width ?? ""),
        depth: String(existing.depth ?? dimensions.depth ?? ""),
        height: String(existing.height ?? dimensions.height ?? ""),
        dimensionUnit: String(existing.dimension_unit ?? candidate.dimension_unit ?? ""),
        confidence: String(existing.confidence ?? "0.00"),
      });
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally {
      setLocalReviewBusy(false);
    }
  }

  async function submitLocalReview(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!job || !localReviewCandidate) return;
    const numberOrNull = (raw: string) => {
      const value = Number(raw);
      return raw.trim() && Number.isFinite(value) ? value : null;
    };
    const confidence = Number(localReviewForm.confidence);
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      setError("复核置信度必须是 0 到 1 之间的数字。");
      return;
    }
    setLocalReviewBusy(true);
    setError(null);
    try {
      await recordLocalAgentReview(job.job_id, localReviewCandidate.candidate_id, {
        eligible: localReviewForm.eligible,
        single_product: localReviewForm.singleProduct,
        background_ok: localReviewForm.backgroundOk,
        image_to_3d_suitable: localReviewForm.imageTo3dSuitable,
        category_group: localReviewForm.categoryGroup.trim(),
        product_type: localReviewForm.productType.trim(),
        style: localReviewForm.style.trim(),
        color: localReviewForm.color.trim(),
        material: localReviewForm.material.trim(),
        feature: localReviewForm.feature.trim(),
        width: numberOrNull(localReviewForm.width),
        depth: numberOrNull(localReviewForm.depth),
        height: numberOrNull(localReviewForm.height),
        dimension_unit: localReviewForm.dimensionUnit.trim(),
        confidence,
        reason_codes: ["LOCAL_AGENT_UI_REVIEW"],
        source_image_vision_consistent: true,
        reviewed_media_sha256: localReviewCandidate.media_sha256,
      }, "local-agent");
      setLocalReviewCandidate(null);
      setLocalReviewForm(EMPTY_LOCAL_REVIEW_FORM);
      setActionMsg("本地 Agent 复核证据已保存；现在可以恢复同一 Job。");
      await load();
    } catch (reason) {
      setError(readableConsoleError(reason));
    } finally {
      setLocalReviewBusy(false);
    }
  }

  if (error) return <div className="console-page"><div className="console-alert console-alert--danger"><strong>无法打开 Job</strong><span>{error}</span><Link href="/jobs">返回任务列表</Link></div></div>;
  if (!job) return <div className="console-page"><div className="console-panel console-loading">正在读取 Job Detail…</div></div>;

  const canResume = Boolean(run && (run.status === "BLOCKED" || job.status === "HUMAN_REQUIRED" || job.status === "TARGET_SHORTAGE"));
  const canResumeScan = Boolean(siteScan && ["HUMAN_REQUIRED", "FAILED", "BROWSER_RUNTIME_NOT_INSTALLED", "PARTIAL", "BRAIN_NOT_CONFIGURED"].includes(siteScan.status));
  const currentStageIndex = stageIndex(job.current_stage);
  const candidateItems = Array.isArray((candidatePool as { items?: unknown }).items) ? (candidatePool as { items: Array<{ candidate_id?: string; product_name?: string; name_char_count?: number; name_limit?: number; state?: string; production_gate_status?: string; review_provider?: string; media_binding_status?: string; production_gate_reasons?: string[] }> }).items : [];
  const latestL2Event = [...events].reverse().find((event) => event.event_type === "HUMAN_REQUIRED" && event.stage === "L2_BROWSER");
  const latestL2Payload = latestL2Event?.payload && typeof latestL2Event.payload === "object" ? latestL2Event.payload as Record<string, unknown> : {};
  const latestL2Url = typeof latestL2Payload.url === "string" && /^https?:\/\//i.test(latestL2Payload.url) ? latestL2Payload.url : null;
  const latestL2IsCandidate = typeof latestL2Payload.candidate_id === "string" && latestL2Payload.candidate_id.length > 0;
  return <div className="console-page">
    <div className="console-breadcrumb"><Link href="/jobs">任务</Link><span>/</span><span>{job.title}</span></div>
    <div className="console-page-header"><div><p className="console-eyebrow">Job Detail / {job.job_id}</p><h1>{job.title}</h1><p className="console-page-description">{job.goal}</p></div><div className="console-header-actions"><span className={`console-status console-status--${job.status === "PRODUCTION_BLOCKED" ? "danger" : job.status === "COMPLETED" ? "success" : "attention"}`}><i />{STATUS[job.status] ?? job.status}</span><Link href="/review" className="console-button console-button--secondary">打开 Review</Link></div></div>
    <section className="job-detail-summary"><div><span>站点</span><strong>{job.site_name}</strong><small>{job.source_url}</small></div><div><span>目标</span><strong>{job.target_mode === "ALL" ? "全部" : job.target_value}</strong><small>{job.scope === "NEW_ONLY" ? "只要新增" : "包含既有"}</small></div><div><span>合格</span><strong>{job.counts.eligible_count}</strong><small>reported {job.counts.reported_count} · unique {job.counts.unique_count}</small></div><div><span>已交付</span><strong>{job.counts.delivered_count}</strong><small>Provider calls {job.provider_calls}</small></div></section>
     <section className="console-panel"><div className="console-section-title"><div><h2>生产控制</h2><p>启动 Website Production Engine；候选池、浏览器会话、Provider ledger 和收据都归属同一 Job。</p></div></div>{actionMsg ? <p className="console-empty-text">{actionMsg}</p> : null}{askCeiling ? <div className="console-alert console-alert--attention"><strong>请输入本次最高建模成本</strong><p className="wizard-inline-note">内嵌环境不支持弹窗，请填写本次任务允许的最高建模成本（人民币元）。审批不会立即产生建模调用。</p><div className="wizard-input-row"><label className="console-field console-field--small"><span>成本上限（元）</span><input className="console-input" type="number" min={1} step={1} value={ceilingRaw} onChange={(event) => setCeilingRaw(event.target.value)} /></label><button type="button" className="console-button console-button--primary" onClick={() => void confirmCeiling()} disabled={starting}>{starting ? "审批并启动中…" : "确认成本上限并启动 →"}</button><button type="button" className="console-button console-button--quiet" onClick={() => setAskCeiling(false)} disabled={starting}>取消</button></div></div> : null}<p className="wizard-inline-note">正常流程不需要填写证据目录。Website 会从已选类目自动采集；遇到 CAPTCHA/访问验证时显示 Human Required，并从同一浏览器会话恢复。</p>{latestL2Url ? <div className="console-alert console-alert--attention"><strong>{latestL2IsCandidate ? "需要处理 L2 商品核对" : "需要处理 L2 发现入口"}</strong><p>{String(latestL2Payload.reason_code ?? "L2_BROWSER_REQUIRED")}：请打开当前入口处理页面访问/验证并保留商品发现证据；完成后恢复同一 Job。</p><a className="console-button console-button--secondary" href={latestL2Url} target="_blank" rel="noreferrer">打开处理入口 ↗</a></div> : null}<div className="wizard-footer"><button type="button" className="console-button console-button--primary" onClick={() => void startRunNow()} disabled={starting || run?.status === "RUNNING"}>{starting ? "启动中…" : run?.status === "RUNNING" ? "生产中…" : "开始生产 →"}</button>{canResume ? <button type="button" className="console-button console-button--secondary" onClick={() => void resumeRunNow()} disabled={starting}>恢复同一 Job →</button> : null}{canResumeScan ? <button type="button" className="console-button console-button--secondary" onClick={() => void resumeScanNow()} disabled={starting}>恢复站点浏览器会话 →</button> : null}</div></section>
    {run ? <section className="console-panel"><div className="console-section-title"><div><h2>生产运行</h2><p>最近一次生产的实时进度与结构化事件（每 5 秒刷新）。</p></div></div><div className="plan-head"><div><span className="console-card-kicker">{run.run_id}</span><h3>{run.stage} · {run.status}</h3><p>{run.items_total ? `${run.items_done} / ${run.items_total}` : run.progress_note ?? "等待 Production Engine 输出…"}</p></div><span className={`console-status console-status--${run.status === "SUCCEEDED" ? "success" : run.status === "RUNNING" ? "attention" : "danger"}`}><i />{run.status === "RUNNING" ? "生产中" : run.status === "SUCCEEDED" ? "完成" : run.status === "BLOCKED" ? "流程已暂停" : "失败"}</span></div>{run.workspace ? <p className="console-footnote">工作区：<code>{run.workspace}</code></p> : null}{run.stdout_tail ? <details className="developer-details"><summary>运行日志（末尾）</summary><pre>{run.stdout_tail}</pre></details> : null}{run.error ? <div className="console-alert console-alert--danger"><strong>失败原因</strong><span>{run.error}</span></div> : null}</section> : null}
    <div className="job-detail-grid"><section className="console-panel"><div className="console-section-title"><div><h2>Pipeline Timeline</h2><p>从业务阶段查看当前位置、完成项与暂停原因。</p></div></div><div className="stage-timeline">{STAGES.map((stage, index) => { const state = index < currentStageIndex ? "完成" : index === currentStageIndex ? (job.status === "RUNNING" ? "进行中" : "当前") : "等待"; return <div className={`stage-timeline-row ${index === currentStageIndex ? "is-current" : ""}`} key={stage.key}><span>{index + 1}</span><div><strong>{stage.label}</strong><small>{index === currentStageIndex ? job.last_reason ?? stage.detail : stage.detail}</small></div><b>{state}</b></div>; })}</div></section><section className="console-panel"><div className="console-section-title"><div><h2>当前状态与安全</h2><p>只显示可操作状态、证据结果和 reason code。</p></div></div><div className="job-policy-kv"><div><span>目标策略</span><strong>{job.target_mode} / {job.allocation_strategy}</strong></div><div><span>短缺策略</span><strong>{job.spillover}</strong></div><div><span>Provider</span><strong>{job.provider}</strong></div><div><span>Safety Gate</span><strong>{job.provider_safety}</strong></div><div><span>Site Scan</span><strong>{siteScan?.status ?? "—"}</strong></div><div><span>Ready Pool</span><strong>{String(job.counts.ready_count ?? candidatePool.target_count ?? 0)}</strong></div></div>{job.last_reason ? <div className={`console-alert ${job.status === "RUNNING" || job.status === "COMPLETED" ? "" : "console-alert--danger"}`}><strong>{job.current_stage}</strong><span>{job.last_reason}</span></div> : null}<details className="developer-details"><summary>查看 Job Policy / Candidate Pool 摘要</summary><pre>{JSON.stringify({ policy: job.policy, candidate_pool: candidatePool }, null, 2)}</pre></details></section></div>
    <section className="console-panel"><div className="console-section-title"><div><h2>最终命名与 Production Gate</h2><p>名称按整词治理，字符数包含空格；超过 50 个字符不能进入建模。</p></div></div>{candidateItems.length ? <div className="console-table-wrap"><table className="console-table"><thead><tr><th>最终名称</th><th>字符数</th><th>候选状态</th><th>媒体绑定</th><th>Review Provider</th><th>Gate</th><th>操作</th></tr></thead><tbody>{candidateItems.map((item) => <tr key={item.candidate_id ?? item.product_name}><td><strong>{item.product_name || "待命名"}</strong></td><td>{item.name_char_count ?? 0} / {item.name_limit ?? 50}</td><td>{item.state ?? "—"}</td><td>{item.media_binding_status ?? "—"}</td><td>{item.review_provider ?? "—"}</td><td><strong>{item.production_gate_status ?? "尚未评估"}</strong>{item.production_gate_reasons?.length ? <small>{item.production_gate_reasons.join("、")}</small> : null}</td><td>{item.candidate_id && item.state === "VISUAL_PENDING" && !item.review_provider ? <button type="button" className="console-row-action" onClick={() => void openLocalReview(item.candidate_id ?? "")} disabled={localReviewBusy}>{localReviewBusy ? "读取中…" : "本地复核"}</button> : "—"}</td></tr>)}</tbody></table></div> : <p className="console-empty-text">候选进入池后会在这里显示最终名称、字符数和 Gate 结果。</p>}
      {localReviewCandidate ? <form className="console-alert console-alert--attention" onSubmit={(event) => void submitLocalReview(event)}><strong>Local Agent · 候选视觉复核</strong><p>请先查看主图和来源页，只提交与当前主图 SHA-256 一致的结构化判断。勾选项全部通过后才会允许候选继续。</p><div style={{ display: "grid", gridTemplateColumns: "minmax(180px, 280px) 1fr", gap: 16, alignItems: "start", marginTop: 12 }}><div><img src={localReviewCandidate.preview_url} alt={localReviewCandidate.source_name || "候选主图"} style={{ width: "100%", maxHeight: 260, objectFit: "contain", background: "#fff", borderRadius: 8 }} /><a className="console-inline-link" href={localReviewCandidate.canonical_url} target="_blank" rel="noreferrer">打开来源页 ↗</a></div><div className="wizard-three-col"><label className="console-field"><span>规范类目</span><input value={localReviewForm.categoryGroup} onChange={(event) => setLocalReviewForm((current) => ({ ...current, categoryGroup: event.target.value }))} required /></label><label className="console-field"><span>产品类型</span><input value={localReviewForm.productType} onChange={(event) => setLocalReviewForm((current) => ({ ...current, productType: event.target.value }))} placeholder="例如 Lounge Chair" /></label><label className="console-field"><span>置信度 0–1</span><input type="number" min="0" max="1" step="0.01" value={localReviewForm.confidence} onChange={(event) => setLocalReviewForm((current) => ({ ...current, confidence: event.target.value }))} required /></label><label className="console-field"><span>风格</span><input value={localReviewForm.style} onChange={(event) => setLocalReviewForm((current) => ({ ...current, style: event.target.value }))} /></label><label className="console-field"><span>颜色</span><input value={localReviewForm.color} onChange={(event) => setLocalReviewForm((current) => ({ ...current, color: event.target.value }))} /></label><label className="console-field"><span>材质</span><input value={localReviewForm.material} onChange={(event) => setLocalReviewForm((current) => ({ ...current, material: event.target.value }))} /></label><label className="console-field"><span>宽</span><input type="number" min="0" step="0.01" value={localReviewForm.width} onChange={(event) => setLocalReviewForm((current) => ({ ...current, width: event.target.value }))} /></label><label className="console-field"><span>深</span><input type="number" min="0" step="0.01" value={localReviewForm.depth} onChange={(event) => setLocalReviewForm((current) => ({ ...current, depth: event.target.value }))} /></label><label className="console-field"><span>高</span><input type="number" min="0" step="0.01" value={localReviewForm.height} onChange={(event) => setLocalReviewForm((current) => ({ ...current, height: event.target.value }))} /></label><label className="console-field"><span>尺寸单位</span><input value={localReviewForm.dimensionUnit} onChange={(event) => setLocalReviewForm((current) => ({ ...current, dimensionUnit: event.target.value }))} placeholder="in / cm / mm" /></label></div></div><div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 14 }}><label><input type="checkbox" checked={localReviewForm.eligible} onChange={(event) => setLocalReviewForm((current) => ({ ...current, eligible: event.target.checked }))} /> 是真实单品</label><label><input type="checkbox" checked={localReviewForm.singleProduct} onChange={(event) => setLocalReviewForm((current) => ({ ...current, singleProduct: event.target.checked }))} /> 主图只有一个产品</label><label><input type="checkbox" checked={localReviewForm.backgroundOk} onChange={(event) => setLocalReviewForm((current) => ({ ...current, backgroundOk: event.target.checked }))} /> 背景适合建模</label><label><input type="checkbox" checked={localReviewForm.imageTo3dSuitable} onChange={(event) => setLocalReviewForm((current) => ({ ...current, imageTo3dSuitable: event.target.checked }))} /> 图片适合 3D</label></div><div className="wizard-footer"><button type="button" className="console-button console-button--quiet" onClick={() => setLocalReviewCandidate(null)} disabled={localReviewBusy}>取消</button><button type="submit" className="console-button console-button--primary" disabled={localReviewBusy}>{localReviewBusy ? "保存中…" : "保存复核"}</button></div></form> : null}</section>
    <section className="console-panel"><div className="console-section-title"><div><h2>类目快照</h2><p>同时保留站点原生名称与规范类目；未验证建议会显式标记。</p></div></div>{categories.length ? <div className="console-table-wrap"><table className="console-table"><thead><tr><th>原生类目</th><th>规范类目</th><th>路径</th><th>Reported</th><th>Eligible</th><th>选择</th></tr></thead><tbody>{categories.map((category) => <tr key={category.category_id}><td>{category.native_name}</td><td><strong>{category.canonical_name}</strong></td><td>{category.path}</td><td>{category.reported_count || "待验证"}</td><td>{category.eligible_count || "—"}</td><td>{category.selected ? "已选" : "—"}</td></tr>)}</tbody></table></div> : <p className="console-empty-text">尚未生成类目快照。</p>}</section>
    <section className="console-panel"><div className="console-section-title"><div><h2>交付收据</h2><p>只有 Canonical Runtime 发出的 DELIVERED Artifact 才可下载。</p></div></div>{artifacts.length ? <div className="event-timeline">{artifacts.map((artifact) => <div key={artifact.artifact_id}><span>↧</span><div><strong>{artifact.artifact_type} · {artifact.status}</strong><small>{artifact.sha256 ?? "等待 SHA-256"} · {artifact.relative_path}</small>{artifact.status === "DELIVERED" ? <a className="console-row-action" href={`${API_ROOT}/control/deliveries/artifacts/${encodeURIComponent(artifact.artifact_id)}/download`}>下载</a> : null}</div></div>)}</div> : <p className="console-empty-text">尚无交付 Artifact。</p>}</section>
    <section className="console-panel"><div className="console-section-title"><div><h2>Provider Ledger</h2><p>已知 task ID 只恢复轮询；SUBMISSION_UNKNOWN 不会自动重提。</p></div></div>{providerTasks.length ? <div className="console-table-wrap"><table className="console-table"><thead><tr><th>Candidate</th><th>Provider Task</th><th>Checkpoint</th><th>POST</th><th>Poll</th><th>Reason</th></tr></thead><tbody>{providerTasks.map((task) => <tr key={task.ledger_id}><td>{task.candidate_id}</td><td>{task.provider_task_id ?? "未确认"}</td><td><strong>{task.checkpoint_state}</strong></td><td>{task.post_attempts}</td><td>{task.poll_attempts}</td><td>{task.error_code ?? "—"}</td></tr>)}</tbody></table></div> : <p className="console-empty-text">尚无 Provider 提交；Provider POST 计数为 {job.provider_calls}。</p>}</section>
    <section className="console-panel"><div className="console-section-title"><div><h2>Live Activity</h2><p>结构化 action / status / evidence result / reason code；不展示模型内部推理。</p></div></div>{events.length ? <div className="event-timeline">{events.map((event, index) => { const payload = (event.payload && typeof event.payload === "object" ? event.payload : {}) as Record<string, unknown>; return <div key={`${String(event.sequence)}-${index}`}><span>{String(event.sequence)}</span><div><strong>{String(event.message)}</strong><small>{String(event.event_type)} · {String(event.status)} · {String(event.stage ?? "—")} · {String(payload.reason_code ?? payload.blocker ?? "OK")}</small></div></div>; })}</div> : <p className="console-empty-text">尚无结构化活动。</p>}</section>
  </div>;
}
