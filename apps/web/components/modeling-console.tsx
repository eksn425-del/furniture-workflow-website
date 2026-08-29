"use client";

import { useCallback, useEffect, useState } from "react";

import {
  createBlenderExportJob,
  createModelGenerationJob,
  getModelingReadiness,
  readableError,
} from "@/lib/api";
import type { ModelingReadiness } from "@/lib/types";

const PROVIDERS = [
  { key: "lux3d", label: "Lux3D" },
  { key: "tripo", label: "Tripo" },
  { key: "hunyuan", label: "腾讯混元" },
] as const;

export function ModelingConsole({ projectId }: { projectId: string }) {
  const [provider, setProvider] = useState<(typeof PROVIDERS)[number]["key"]>("lux3d");
  const [readiness, setReadiness] = useState<ModelingReadiness | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [estimatedCost, setEstimatedCost] = useState("");
  const [costCeiling, setCostCeiling] = useState("");
  const [approved, setApproved] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReadiness(await getModelingReadiness(projectId, provider));
    } catch (loadError) {
      setError(readableError(loadError));
    } finally {
      setLoading(false);
    }
  }, [projectId, provider]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  async function submitPaidGate() {
    if (!readiness?.next_gate || !readiness.locked_catalog_sha256) return;
    const estimateMinor = Math.round(Number(estimatedCost) * 100);
    const ceilingMinor = Math.round(Number(costCeiling) * 100);
    if (!Number.isFinite(estimateMinor) || !Number.isFinite(ceilingMinor)) {
      setError("请填写有效的预计费用和最高费用。");
      return;
    }
    setSubmitting(true);
    setError(null);
    setQueuedMessage(null);
    try {
      const lockToken = readiness.locked_catalog_sha256.slice(0, 20);
      const job = await createModelGenerationJob(projectId, {
        provider,
        requested_count: readiness.next_gate,
        catalog_lock_sha256: readiness.locked_catalog_sha256,
        estimated_cost_minor: estimateMinor,
        approved_cost_ceiling_minor: ceilingMinor,
        currency: "CNY",
        approval_confirmed: approved,
        idempotency_key: `model:${provider}:${lockToken}:${readiness.next_gate}:${ceilingMinor}`,
      });
      setQueuedMessage(`已创建 ${readiness.next_gate} 件完整批次任务：${job.job_id}`);
      await load();
    } catch (submitError) {
      setError(readableError(submitError));
    } finally {
      setSubmitting(false);
    }
  }

  async function submitBlenderExport() {
    if (!readiness?.locked_catalog_sha256) return;
    setSubmitting(true);
    setError(null);
    setQueuedMessage(null);
    try {
      const job = await createBlenderExportJob(
        projectId,
        readiness.locked_catalog_sha256,
      );
      setQueuedMessage(`Blender 修复与 GLB 导出任务已创建：${job.job_id}`);
      await load();
    } catch (submitError) {
      setError(readableError(submitError));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="modeling-console" aria-labelledby="modeling-title">
      <div className="modeling-console__header">
        <div>
          <p className="eyebrow">SKILLS MIRROR / 03 MODELS</p>
          <h2 id="modeling-title">Agent 建模工作流</h2>
        </div>
        <p>
          严格复刻 Skills 的 QA、图片准备、付费生成、Blender 修复和 GLB
          导出顺序；任何缺少的关卡都会自动阻断。
        </p>
      </div>

      <div className="provider-selector" role="group" aria-label="模型供应商">
        {PROVIDERS.map((item) => {
          const state = readiness?.providers.find((entry) => entry.provider === item.key);
          return (
            <button
              key={item.key}
              type="button"
              className={provider === item.key ? "is-selected" : ""}
              onClick={() => setProvider(item.key)}
            >
              <span>{item.label}</span>
              <small>{state?.configured ? "密钥已配置" : "等待配置"}</small>
            </button>
          );
        })}
      </div>

      {error ? <div className="inline-sync-error" role="alert">{error}</div> : null}

      <div className="modeling-readiness-grid" aria-busy={loading}>
        <article>
          <span>目录锁</span>
          <strong>{readiness?.catalog_lock_status ?? (loading ? "检查中" : "未知")}</strong>
          <small>
            {readiness?.record_count ?? 0} 件锁定商品 / {readiness?.catalog_lock_format ?? "none"}
          </small>
        </article>
        <article>
          <span>本次完整目标</span>
          <strong>
            {readiness?.generation_complete
              ? "已完成全部生成"
              : readiness?.next_gate
                ? `${readiness.next_gate} 件`
                : "等待目录锁"}
          </strong>
          <small>
            SAFE_READY {readiness?.safe_ready_count ?? 0} / 已生成 {readiness?.generated_model_count ?? 0}
          </small>
        </article>
        <article>
          <span>付费生成</span>
          <strong>
            {readiness?.generation_complete
              ? "已有完整成果"
              : readiness?.can_generate_models
                ? "可申请启动"
                : "安全关闭"}
          </strong>
          <small>需要目录锁、供应商密钥和费用审批</small>
        </article>
        <article>
          <span>Blender / GLB</span>
          <strong>
            {readiness?.export_complete
              ? "最终交付已完成"
              : readiness?.blender_ready
                ? "Worker 就绪"
                : "尚未接通"}
          </strong>
          <small>最终 GLB {readiness?.final_model_count ?? 0} / 原始模型与修复模型分开</small>
        </article>
      </div>

      <ol className="modeling-stage-line" aria-label="模型工作流阶段">
        {[
          ["QA", readiness?.can_prepare_images],
          ["图片", readiness?.can_prepare_images],
          ["生成", readiness?.generation_complete || readiness?.can_generate_models],
          ["Blender", readiness?.blender_complete || readiness?.can_run_blender],
          ["GLB 导出", readiness?.export_complete || readiness?.can_export],
        ].map(([label, ready], index) => (
          <li key={String(label)} className={ready ? "is-ready" : ""}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <strong>{String(label)}</strong>
          </li>
        ))}
      </ol>

      {readiness?.can_generate_models && readiness.next_gate ? (
        <div className="modeling-approval">
          <div>
            <span className="panel-index">EXACT BATCH / {readiness.next_gate} ITEMS</span>
            <h3>确认本轮付费模型生成</h3>
            <p>
              这里一次批准锁定目录中的 {readiness.next_gate} 件完整目标，不拆成 1/3/20。目录内容变化后，批准会自动失效。
            </p>
          </div>
          <label>
            <span>供应商给出的预计费用（元）</span>
            <input
              inputMode="decimal"
              value={estimatedCost}
              onChange={(event) => setEstimatedCost(event.target.value)}
              placeholder="例如 120.00"
            />
          </label>
          <label>
            <span>本轮最高允许费用（元）</span>
            <input
              inputMode="decimal"
              value={costCeiling}
              onChange={(event) => setCostCeiling(event.target.value)}
              placeholder="必须不低于预计费用"
            />
          </label>
          <label className="modeling-approval__check">
            <input
              type="checkbox"
              checked={approved}
              onChange={(event) => setApproved(event.target.checked)}
            />
            <span>我确认按上述数量和费用上限创建付费任务</span>
          </label>
          <button
            type="button"
            onClick={() => void submitPaidGate()}
            disabled={!approved || submitting}
          >
            {submitting ? "正在创建…" : `批准完整 ${readiness.next_gate} 件任务`}
          </button>
        </div>
      ) : null}

      {readiness?.can_run_blender ? (
        <div className="modeling-ready-banner">
          <div>
            <strong>原始模型已完整对账，可以进入自动修复</strong>
            <span>网站将校正尺寸与朝向、导出最终 GLB，并按每 20 件生成交付批次。</span>
          </div>
          <button
            type="button"
            onClick={() => void submitBlenderExport()}
            disabled={submitting}
          >
            {submitting ? "正在创建…" : "开始 Blender 修复与导出"}
          </button>
        </div>
      ) : null}

      {queuedMessage ? <div className="modeling-ready-banner"><strong>{queuedMessage}</strong></div> : null}

      {readiness?.blockers.length ? (
        <div className="modeling-blockers">
          <div>
            <span className="panel-index">AUTOMATIC GATES</span>
            <h3>当前不能继续的原因</h3>
          </div>
          <ul>
            {readiness.blockers.map((blocker) => (
              <li key={blocker.code}>
                <code>{blocker.code}</code>
                <span>{blocker.message}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="modeling-ready-banner">
          <strong>模型阶段前置条件已满足</strong>
          <span>下一步将进入有费用估算和明确审批的完整 {readiness?.next_gate} 件批次任务。</span>
        </div>
      )}

      <footer>
        <span>{readiness?.workflow_version ?? "living-catalog-to-glb.v8.8.1"}</span>
        <span>Skills {readiness?.skills_bundle_version ?? "7.29.1"}</span>
        <span>{readiness?.model_filename_rule ?? "brand-prefix.v1"}</span>
      </footer>
    </section>
  );
}
