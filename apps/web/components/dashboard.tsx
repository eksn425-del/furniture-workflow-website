"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CreateProjectForm } from "@/components/create-project-form";
import { API_BASE_URL, getCapabilities, getHealth, getProjects, readableError } from "@/lib/api";
import type { CapabilitiesResponse, HealthResponse, ProjectListResponse } from "@/lib/types";
import { findStageDefinition, formatDateTime } from "@/lib/workflow";

interface DashboardState {
  loading: boolean;
  health: HealthResponse | null;
  capabilities: CapabilitiesResponse | null;
  projects: ProjectListResponse | null;
  error: string | null;
}

const INITIAL: DashboardState = {
  loading: true,
  health: null,
  capabilities: null,
  projects: null,
  error: null,
};

const DEMO_MODE = process.env.NEXT_PUBLIC_DEMO_MODE === "true";

async function readDashboard(): Promise<DashboardState> {
  if (DEMO_MODE) {
    const [health, capabilities] = await Promise.allSettled([
      getHealth(), getCapabilities(),
    ]);
    const failure = [health, capabilities].find((item) => item.status === "rejected");
    return {
      loading: false,
      health: health.status === "fulfilled" ? health.value : null,
      capabilities: capabilities.status === "fulfilled" ? capabilities.value : null,
      projects: { items: [], total: 0 },
      error: failure?.status === "rejected" ? readableError(failure.reason) : null,
    };
  }
  const [health, capabilities, projects] = await Promise.allSettled([
    getHealth(), getCapabilities(), getProjects(),
  ]);
  const failure = [health, capabilities, projects].find((item) => item.status === "rejected");
  return {
    loading: false,
    health: health.status === "fulfilled" ? health.value : null,
    capabilities: capabilities.status === "fulfilled" ? capabilities.value : null,
    projects: projects.status === "fulfilled" ? projects.value : null,
    error: failure?.status === "rejected" ? readableError(failure.reason) : null,
  };
}

function stateLabel(status: string) {
  if (status === "completed" || status === "succeeded") return "已完成";
  if (status === "blocked" || status === "failed") return "有异常";
  return "处理中";
}

export function Dashboard() {
  const [state, setState] = useState(INITIAL);
  const refresh = useCallback(async () => setState(await readDashboard()), []);

  useEffect(() => {
    let cancelled = false;
    void readDashboard().then((next) => { if (!cancelled) setState(next); });
    return () => { cancelled = true; };
  }, []);

  const projects = state.projects?.items ?? [];
  const active = projects.filter((project) => !["completed", "succeeded"].includes(project.status));
  const outputs = projects.filter((project) => ["completed", "succeeded"].includes(project.status));
  const online = state.health?.status === "ok";

  return (
    <div className="workbench">
      <section className="workbench-intro">
        <div>
          <h1>一次输入，自动完成</h1>
          <p>给链接和需求，Website Native Runtime 自动去重、筛选白底单品、核对尺寸、生成模型并检查。</p>
        </div>
        <span className={`availability ${online ? "is-online" : ""}`}>
          <i />{state.loading ? "检查中" : online ? "系统可用" : "连接异常"}
        </span>
      </section>

      <nav className="three-steps" aria-label="工作流程">
        <strong><span>1</span>输入</strong><i />
        <span><b>2</b>处理</span><i />
        <span><b>3</b>输出</span>
      </nav>

      {state.error ? <p className="form-error" role="alert">{state.error}</p> : null}

      {!DEMO_MODE ? (
        <section className="surface input-surface">
          <div className="section-heading">
            <h2>输入任务</h2>
            <p>只需填写官网地址和你想完成的工作。模型与数量写在需求里即可。</p>
          </div>
          <CreateProjectForm />
        </section>
      ) : null}

      {!DEMO_MODE ? <div className="result-grid">
        <section className="surface">
          <div className="section-heading">
            <h2>处理中</h2>
            <p>系统自动推进；只在证据确实不足时列出异常。</p>
          </div>
          <div className="task-list">
            {state.loading && !state.projects ? <p className="empty">正在读取任务…</p> : null}
            {!state.loading && active.length === 0 ? <p className="empty">当前没有处理中任务。</p> : null}
            {active.map((project) => (
              <Link href={`/projects/${encodeURIComponent(project.project_id)}`} key={project.project_id}>
                <span className={`state-dot state-${project.status}`} />
                <div><strong>{project.name}</strong><small>{findStageDefinition(project.current_stage)?.label ?? "准备中"}</small></div>
                <em>{stateLabel(project.status)}</em>
              </Link>
            ))}
          </div>
        </section>

        <section className="surface">
          <div className="section-heading">
            <h2>输出</h2>
            <p>完成结果集中在这里；点击任务即可统一下载。</p>
          </div>
          <div className="output-list">
            {outputs.length === 0 ? <p className="empty">完成后，表格、模型和报告会显示在这里。</p> : null}
            {outputs.map((project) => (
              <Link href={`/projects/${encodeURIComponent(project.project_id)}`} key={project.project_id}>
                <div><strong>{project.name}</strong><small>更新于 {formatDateTime(project.updated_at)}</small></div>
                <span>打开 →</span>
              </Link>
            ))}
          </div>
        </section>
      </div> : null}

      <details className="system-settings">
        <summary>系统设置与接口状态</summary>
        <div>
          <p><span>网站接口</span><strong>{API_BASE_URL}</strong></p>
          <p><span>AI 大脑</span><strong>{state.capabilities?.vision_provider === "minimax" && state.capabilities.vision_ready ? "MiniMax 已就绪" : state.capabilities?.qwen_configured ? "千问已配置" : "未配置"}</strong></p>
          <p><span>尺寸识别</span><strong>{state.capabilities?.size_from_picture_key_configured ? "专用密钥已保存" : "使用千问识别"}</strong></p>
          <p><span>3D 生成</span><strong>{state.capabilities?.modeling_enabled ? "已启用" : "未启用"}</strong></p>
          <p><span>Blender</span><strong>{state.capabilities?.blender_worker_ready ? "可用" : "未就绪"}</strong></p>
          <p><span>永久去重总账</span><strong>{state.capabilities?.cgtrader_registry_ready ? `${state.capabilities.cgtrader_generated_product_records} 条产品 / ${state.capabilities.cgtrader_unique_block_keys} 个拦截键` : "未就绪（CGTrader 将安全停止）"}</strong></p>
          <button type="button" onClick={() => void refresh()} disabled={state.loading}>刷新状态</button>
        </div>
      </details>
    </div>
  );
}
