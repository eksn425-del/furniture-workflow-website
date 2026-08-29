"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { createWorkflowOrder, readableError } from "@/lib/api";
import type { CreateWorkflowOrderInput } from "@/lib/types";

function validateUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "请输入 http 或 https 开头的官网地址。";
    }
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
    const path = parsed.pathname.toLowerCase().replace(/\/$/, "");
    if (host === "cgtrader.com" && !(
      path === "/free-3d-models/furniture" || path.startsWith("/free-3d-models/furniture/")
      || path === "/3d-models/furniture" || path.startsWith("/3d-models/furniture/")
    )) {
      return "CGTrader 任务必须从 Furniture 分类列表开始，不能使用全站 Free 3D Models。";
    }
    return null;
  } catch {
    return "官网地址格式不正确。";
  }
}

export function CreateProjectForm() {
  const router = useRouter();
  const [sourceUrl, setSourceUrl] = useState("");
  const [instruction, setInstruction] = useState("");
  const [count, setCount] = useState(3);
  const [provider, setProvider] =
    useState<CreateWorkflowOrderInput["provider"]>("lux3d");
  const [outputMode, setOutputMode] =
    useState<CreateWorkflowOrderInput["output_mode"]>("glb");
  const [name, setName] = useState("");
  const [brandName, setBrandName] = useState("");
  const [siteKey, setSiteKey] = useState("");
  const [useAgentAdapter, setUseAgentAdapter] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function updateInstruction(value: string) {
    setInstruction(value);
    const match = value.match(/(\d{1,3})\s*(?:个|件|款|条)/);
    if (match) setCount(Math.min(500, Math.max(1, Number(match[1]))));
    if (/tripo/i.test(value)) setProvider("tripo");
    else if (/混元|hunyuan/i.test(value)) setProvider("hunyuan");
    else if (/lux\s*3d/i.test(value)) setProvider("lux3d");
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const url = sourceUrl.trim();
    const urlError = validateUrl(url);
    if (urlError) return setError(urlError);
    if (instruction.trim().length < 4) return setError("请用一句话说明要抓取和产出什么。");
    if (count < 1 || count > 500) return setError("商品数量需要在 1 到 500 之间。");

    setSubmitting(true);
    try {
      const isCgTrader = new URL(url).hostname.toLowerCase().replace(/^www\./, "") === "cgtrader.com";
      const order = await createWorkflowOrder({
        source_url: url,
        instruction: instruction.trim(),
        product_count: count,
        max_pages: 50,
        provider,
        output_mode: outputMode,
        use_agent_adapter: useAgentAdapter,
        ...(isCgTrader ? {
          authorization_mode: "EXACT_COUNT_AUTHORIZATION",
          progressive_gates: [1, 3, 10, 20],
          category_quotas: {},
        } : {}),
        ...(name.trim() ? { name: name.trim() } : {}),
        ...(brandName.trim() ? { brand_name: brandName.trim() } : {}),
        ...(siteKey.trim() ? { site_key: siteKey.trim() } : {}),
      });
      router.push(`/projects/${encodeURIComponent(order.project_id)}`);
    } catch (reason) {
      setError(readableError(reason));
      setSubmitting(false);
    }
  }

  return (
    <form className="order-form" onSubmit={submit} noValidate>
      <div className="order-fields">
        <label className="field field-wide">
          <span>官网地址</span>
          <input
            type="url"
            value={sourceUrl}
            onChange={(event) => setSourceUrl(event.target.value)}
            placeholder="https://品牌官网/商品分类"
            disabled={submitting}
            required
          />
        </label>
        <label className="field field-count">
          <span>商品数量</span>
          <input
            type="number"
            min={1}
            max={500}
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
            disabled={submitting}
          />
        </label>
      </div>

      <label className="field">
        <span>任务需求</span>
        <textarea
          value={instruction}
          onChange={(event) => updateInstruction(event.target.value)}
          placeholder="例如：抓取这个品牌的椅子，使用 Lux3D 生成 100 个 GLB，自动完成尺寸核对、Blender 修复和最终质量检查。"
          rows={4}
          maxLength={2000}
          disabled={submitting}
          required
        />
      </label>

      <div className="order-options">
        <label>
          <span>3D 供应商</span>
          <select
            value={provider}
            onChange={(event) => setProvider(event.target.value as CreateWorkflowOrderInput["provider"])}
            disabled={submitting || outputMode === "catalog"}
          >
            <option value="lux3d">Lux3D</option>
            <option value="tripo">Tripo</option>
            <option value="hunyuan">腾讯混元</option>
          </select>
        </label>
        <label>
          <span>最终产出</span>
          <select
            value={outputMode}
            onChange={(event) => setOutputMode(event.target.value as CreateWorkflowOrderInput["output_mode"])}
            disabled={submitting}
          >
            <option value="glb">GLB + 表格 + 报告</option>
            <option value="catalog">只要标准商品目录</option>
          </select>
        </label>
      </div>

      <details className="advanced-settings">
        <summary>高级选项（通常不用填写）</summary>
        <div className="advanced-grid">
          <label className="field"><span>任务名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="默认使用网站名称" /></label>
          <label className="field"><span>品牌</span><input value={brandName} onChange={(event) => setBrandName(event.target.value)} placeholder="自动识别" /></label>
          <label className="field"><span>站点适配器</span><input value={siteKey} onChange={(event) => setSiteKey(event.target.value)} placeholder="自动识别" /></label>
          <label className="check-field"><input type="checkbox" checked={useAgentAdapter} onChange={(event) => setUseAgentAdapter(event.target.checked)} /><span>强制使用已验证的专用站点适配器</span></label>
        </div>
      </details>

      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <div className="order-submit">
        <p>提交即确认本次网站、品类、供应商与数量范围；系统不会重复询问。</p>
        <button type="submit" disabled={submitting || !sourceUrl.trim() || !instruction.trim()}>
          {submitting ? "正在创建任务…" : "开始自动处理 →"}
        </button>
      </div>
    </form>
  );
}
