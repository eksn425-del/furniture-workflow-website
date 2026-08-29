"use client";

import { useEffect, useState } from "react";
import {
  createQualityCheckJob,
  getEnterpriseInput,
  readableError,
  uploadEnterpriseInput,
} from "@/lib/api";
import type { EnterpriseInput, WorkflowJob } from "@/lib/types";
import { StatusChip } from "@/components/status-chip";


export function EnterpriseQaConsole({
  projectId,
  jobs,
  onChanged,
}: {
  projectId: string;
  jobs: WorkflowJob[];
  onChanged: () => Promise<void>;
}) {
  const [enterprise, setEnterprise] = useState<EnterpriseInput | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [useDino, setUseDino] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void getEnterpriseInput(projectId)
      .then((value) => { if (active) setEnterprise(value); })
      .catch((reason) => { if (active) setError(readableError(reason)); });
    return () => { active = false; };
  }, [projectId]);

  const latestQa = jobs.find((job) => job.task_key === "library_comparison");
  const qaActive = latestQa && ["pending", "running"].includes(latestQa.status);

  async function upload() {
    if (!file) return;
    setPending(true);
    setError(null);
    try {
      const receipt = await uploadEnterpriseInput(projectId, file);
      setEnterprise(receipt);
      setFile(null);
      await onChanged();
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setPending(false);
    }
  }

  async function startQa() {
    if (!enterprise) return;
    setPending(true);
    setError(null);
    try {
      await createQualityCheckJob(projectId, enterprise.sha256, useDino);
      await onChanged();
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="enterprise-qa" aria-labelledby="enterprise-qa-title">
      <header>
        <div>
          <p className="eyebrow">05 INPUT → 06 FINAL QUALITY GATE</p>
          <h2 id="enterprise-qa-title">企业商品库与最终质量检查</h2>
        </div>
        <p>
          上传酷家乐等企业系统导出的表格。网站会保留原文件，不覆盖、不改写；随后逐件核对身份、预览图和官网原图。
        </p>
      </header>

      {error ? <div className="inline-sync-error" role="alert"><strong>操作未完成</strong><span>{error}</span></div> : null}

      <div className="enterprise-qa__grid">
        <article>
          <span className="panel-index">04 / IMMUTABLE INPUT</span>
          {enterprise ? (
            <div className="enterprise-receipt">
              <strong>{enterprise.original_filename}</strong>
              <span>{(enterprise.size_bytes / 1024).toFixed(1)} KB · 已锁定原始文件</span>
              <code>{enterprise.sha256.slice(0, 20)}…</code>
            </div>
          ) : (
            <>
              <label className="enterprise-file">
                <span>选择企业商品库（.xlsx 或 .csv，最大 25 MB）</span>
                <input
                  type="file"
                  accept=".xlsx,.csv"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                />
              </label>
              <button type="button" onClick={() => void upload()} disabled={!file || pending}>
                {pending ? "正在保存…" : "安全上传并锁定"}
              </button>
            </>
          )}
        </article>

        <article>
          <div className="enterprise-qa__status">
            <span className="panel-index">05 / CLIP + DINO</span>
            {latestQa ? <StatusChip status={latestQa.status} /> : null}
          </div>
          <label className="check-row">
            <input type="checkbox" checked={useDino} onChange={(event) => setUseDino(event.target.checked)} />
            <span>启用双重视觉检查（更慢，但更适合最终交付）</span>
          </label>
          <button
            type="button"
            onClick={() => void startQa()}
            disabled={!enterprise || pending || Boolean(qaActive) || latestQa?.status === "succeeded"}
          >
            {qaActive ? "质量检查运行中…" : "开始最终质量检查"}
          </button>
          <p>任何重名、漏匹配、图片失效或低相似度都会阻止交付，不会自动猜测为通过。</p>
        </article>
      </div>
    </section>
  );
}
