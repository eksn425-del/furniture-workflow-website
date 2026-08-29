"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  advanceWorkflowOrder,
  apiDownloadUrl,
  cancelWorkflowJob,
  getProjectJobs,
  getModelTaskMonitor,
  getScrapeEvidence,
  getWorkflowOrder,
  readableError,
  retryWorkflowJob,
  setGovernedName,
} from "@/lib/api";
import type { ProviderTaskMonitor, ScrapeEvidence, WorkflowJob, WorkflowOrder } from "@/lib/types";
import { formatDateTime } from "@/lib/workflow";

const STEP_LABELS: Record<string, string> = {
  "01_scrape": "网页资料抓取",
  "02_repair": "尺寸识别与核对",
  "03_models": "3D 生成与 Blender 修复",
  "04_export": "可上传模型导出",
  "05_input": "企业资料输入（可选）",
  "06_qa": "最终质量检查",
};

function readableStatus(status: string) {
  if (status === "succeeded" || status === "completed") return "已完成";
  if (status === "running" || status === "active") return "处理中";
  if (status === "blocked" || status === "failed") return "有异常";
  if (status === "ready") return "准备中";
  return "等待";
}

function collectArtifactPaths(jobs: WorkflowJob[]): string[] {
  const paths = new Set<string>();
  for (const job of jobs) {
    const artifacts = job.result?.artifacts;
    if (!artifacts || typeof artifacts !== "object" || Array.isArray(artifacts)) continue;
    for (const value of Object.values(artifacts)) {
      if (typeof value === "string" && !value.startsWith("http")) paths.add(value);
    }
  }
  return [...paths];
}

export function ProjectWorkspace({ projectId }: { projectId: string }) {
  const [order, setOrder] = useState<WorkflowOrder | null>(null);
  const [jobs, setJobs] = useState<WorkflowJob[]>([]);
  const [evidence, setEvidence] = useState<ScrapeEvidence | null>(null);
  const [modelTasks, setModelTasks] = useState<ProviderTaskMonitor | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nameEdits, setNameEdits] = useState<Record<string, string>>({});

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [orderResult, jobsResult, evidenceResult, modelTaskResult] = await Promise.all([
        getWorkflowOrder(projectId),
        getProjectJobs(projectId),
        getScrapeEvidence(projectId),
        getModelTaskMonitor(projectId),
      ]);
      setOrder(orderResult);
      setJobs(jobsResult.items);
      setEvidence(evidenceResult);
      setModelTasks(modelTaskResult);
      setError(null);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (!order || ["completed", "blocked", "failed"].includes(order.status)) return;
    const timer = window.setInterval(() => void load(true), 2500);
    return () => window.clearInterval(timer);
  }, [load, order]);

  async function resume() {
    setPending(true);
    try {
      const blockedDiscovery = jobs.find(
        (job) => job.task_key === "product_discovery" && job.status === "blocked",
      );
      if (blockedDiscovery) {
        await retryWorkflowJob(projectId, blockedDiscovery.job_id);
      }
      setOrder(await advanceWorkflowOrder(projectId));
      await load(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setPending(false);
    }
  }

  async function pause() {
    const jobId = order?.active_job?.job_id;
    if (!jobId) return;
    setPending(true);
    try {
      await cancelWorkflowJob(projectId, jobId);
      await load(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setPending(false);
    }
  }

  async function resolveName(recordId: string, currentName: string) {
    const governedName = (nameEdits[recordId] ?? currentName).trim();
    if (!governedName) return;
    setPending(true);
    try {
      await setGovernedName(projectId, recordId, governedName);
      setOrder(await advanceWorkflowOrder(projectId));
      await load(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setPending(false);
    }
  }

  const downloads = useMemo(() => {
    const items = new Map<string, string>();
    if (evidence?.available) {
      for (const [name, path] of Object.entries(evidence.downloads)) items.set(name, path);
    }
    for (const path of collectArtifactPaths(jobs)) {
      const name = path.split(/[\\/]/).pop() || path;
      items.set(name, path);
    }
    return [...items.entries()];
  }, [evidence, jobs]);

  if (loading && !order) return <p className="page-message">正在读取任务…</p>;
  if (!order) {
    return (
      <div className="page-message">
        <p>{error || "这个任务不存在或不是自动工作流任务。"}</p>
        <Link href="/">返回工作台</Link>
      </div>
    );
  }

  return (
    <div className="project-view">
      <nav className="breadcrumb"><Link href="/">工作台</Link><span>/</span><span>{order.project.name}</span></nav>
      <header className="project-title">
        <div>
          <p>{order.provider.toUpperCase()} · {order.product_count} 件 · {order.output_mode === "glb" ? "完整 GLB 交付" : "标准目录"}</p>
          <h1>{order.project.name}</h1>
          <span>{order.instruction}</span>
        </div>
        <div className={`order-state state-${order.status}`}>
          <i />{readableStatus(order.status)}
        </div>
      </header>

      <div className="workflow-controls" aria-label="工作流控制">
        {order.active_job && !["succeeded", "completed", "failed", "blocked", "cancelled"].includes(order.active_job.status) ? (
          <button type="button" onClick={() => void pause()} disabled={pending}>安全暂停</button>
        ) : null}
        {order.status === "cancelled" || order.status === "blocked" ? (
          <button type="button" onClick={() => void resume()} disabled={pending}>继续任务</button>
        ) : null}
        <button type="button" onClick={() => void load()} disabled={loading}>重新读取状态</button>
      </div>

      {error ? <p className="form-error" role="alert">{error}</p> : null}

      <div className="project-columns">
        <section className="surface">
          <div className="section-heading">
            <h2>处理中</h2>
            <p>{order.message}</p>
          </div>
          <ol className="workflow-rows">
            {order.project.stages.map((stage) => (
              <li key={stage.key} className={`row-${stage.status}`}>
                <span className="state-dot" />
                <div>
                  <strong>{STEP_LABELS[stage.key] ?? stage.label}</strong>
                  <small>{stage.completed_tasks} / {stage.total_tasks} 个步骤</small>
                </div>
                <em>{readableStatus(stage.status)}</em>
              </li>
            ))}
          </ol>
          {modelTasks?.total ? (
            <div className="provider-monitor">
              <div className="provider-monitor__summary">
                <strong>模型监控：共 {modelTasks.total} 件</strong>
                <span>生成中 {modelTasks.active} · 等待 {modelTasks.waiting} · 完成 {modelTasks.completed} · 异常 {modelTasks.failed + modelTasks.blocked} · Provider 槽位 {modelTasks.active_provider_tasks}/{modelTasks.max_provider_slots} · 未决 {modelTasks.unresolved_provider_tasks}</span>
              </div>
              <div className="provider-monitor__list">
                {modelTasks.items.map((item, index) => (
                  <div key={item.filename}>
                    <span>{index + 1}</span>
                    <strong title={item.filename}>{item.filename}{item.record_id ? ` · ${item.record_id}` : ""}</strong>
                    <code>{item.provider_task_id || "—"}</code>
                    <em title={item.reason || item.checkpoint_state || undefined}>{item.status}{typeof item.provider_progress === "number" ? ` ${item.provider_progress}%` : ""}</em>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
          {evidence?.naming_conflicts.length ? (
            <div className="name-conflicts">
              <div>
                <strong>发现名称冲突</strong>
                <p>系统没有擅自合并不同商品。确认下面的区分名称后，会自动重新核对并继续。</p>
              </div>
              {evidence.naming_conflicts.map((conflict) => (
                <label key={conflict.record_id} className="name-conflict-row">
                  <span>{conflict.original_name}{conflict.sku ? ` · ${conflict.sku}` : ""}</span>
                  <div>
                    <input
                      aria-label={`${conflict.original_name} 的区分名称`}
                      value={nameEdits[conflict.record_id] ?? conflict.governed_name}
                      onChange={(event) => setNameEdits((current) => ({
                        ...current,
                        [conflict.record_id]: event.target.value,
                      }))}
                    />
                    <button
                      type="button"
                      disabled={pending}
                      onClick={() => void resolveName(conflict.record_id, conflict.governed_name)}
                    >保存并继续</button>
                  </div>
                </label>
              ))}
            </div>
          ) : null}
          {order.status === "blocked" ? (
            <button className="resume-button" type="button" onClick={() => void resume()} disabled={pending}>
              {pending ? "正在检查…" : "问题处理后继续"}
            </button>
          ) : null}
        </section>

        <section className="surface">
          <div className="section-heading">
            <h2>输出</h2>
            <p>文件会随着流程完成逐步出现，最终在这里统一下载。</p>
          </div>
          <div className="download-list">
            {downloads.length === 0 ? <p className="empty">当前还没有可下载结果。</p> : null}
            {downloads.map(([name, path]) => (
              <a href={apiDownloadUrl(path)} key={`${name}-${path}`}>
                <div><strong>{name.replaceAll("_", " ")}</strong><small>已生成并记录校验信息</small></div>
                <span>下载 ↓</span>
              </a>
            ))}
          </div>
          {order.exceptions.length ? (
            <div className="exception-box">
              <strong>异常 {order.exceptions.length} 项</strong>
              <ul>{order.exceptions.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          ) : (
            <p className="no-exception">当前没有需要人工处理的异常。</p>
          )}
        </section>
      </div>

      <details className="system-settings">
        <summary>详细记录</summary>
        <div className="record-grid">
          <p><span>项目 ID</span><code>{order.project_id}</code></p>
          <p><span>范围确认</span><strong>{order.scope_authorized ? "已确认" : "未确认"}</strong></p>
          <p><span>创建时间</span><strong>{formatDateTime(order.created_at)}</strong></p>
          <p><span>最近更新</span><strong>{formatDateTime(order.updated_at)}</strong></p>
          <p><span>任务数量</span><strong>{jobs.length}</strong></p>
          <p><span>总账跳过</span><strong>{evidence?.global_registry_skip_count ?? 0} 个已记录产品</strong></p>
          <p><span>产品时限</span><strong>{Number(evidence?.selection_policy?.max_age_years ?? 0) > 0 ? `近 ${String(evidence?.selection_policy?.max_age_years)} 年（缺日期即阻断）` : "未启用"}</strong></p>
          <button type="button" onClick={() => void load()} disabled={loading}>刷新记录</button>
        </div>
      </details>
    </div>
  );
}
