import type { StageSummary, WorkflowStatus } from "@/lib/types";

export interface StageDefinition {
  order: string;
  key: string;
  aliases: string[];
  label: string;
  english: string;
  description: string;
  output: string;
}

export const STAGE_DEFINITIONS: StageDefinition[] = [
  {
    order: "01",
    key: "scrape",
    aliases: ["scrape", "01_scrape", "ingest"],
    label: "爬取",
    english: "ACQUIRE",
    description: "建立产品身份，分开采集商品图、工程图与页面证据。",
    output: "记录 · 资产 · 来源",
  },
  {
    order: "02",
    key: "repair",
    aliases: ["repair", "02_repair", "resolution", "catalog_lock"],
    label: "修复",
    english: "RESOLVE",
    description: "双 AI 独立读图，自动处理冲突并通过策略门禁锁定目录。",
    output: "W/D/H · 决议 · 目录锁",
  },
  {
    order: "03",
    key: "models",
    aliases: ["model", "models", "03_models", "modeling"],
    label: "模型",
    english: "BUILD",
    description: "仅消费已批准的目录锁，保留原始与修复模型。",
    output: "原始模型 · 修复模型",
  },
  {
    order: "04",
    key: "export",
    aliases: ["export", "04_export", "glb_validation"],
    label: "可上传模型",
    english: "EXPORT",
    description: "验证 GLB、材质、轴向和真实尺寸，按批次生成上传文件。",
    output: "最终 GLB · 导出收据",
  },
  {
    order: "05",
    key: "input",
    aliases: ["input", "enterprise_input", "library_input", "05_input"],
    label: "商品库输入",
    english: "IMPORT",
    description: "上传修复模型后，接收商品库导出的表格并锁定一一映射。",
    output: "导出表 · 身份映射",
  },
  {
    order: "06",
    key: "qa",
    aliases: [
      "qa",
      "06_qa",
      "validation",
      "validate",
      "library_check",
      "library",
      "comparison",
      "publish",
    ],
    label: "质量验证",
    english: "VERIFY",
    description: "对照商品库预览、原商品图与目录锁完成 AI 视觉验收。",
    output: "相似度 · 差异 · 验收结果",
  },
];

function normalized(value: string): string {
  return value.trim().toLowerCase().replaceAll("-", "_");
}

export function findStageDefinition(
  key: string | null | undefined,
): StageDefinition | undefined {
  if (!key) return undefined;
  const candidate = normalized(key);
  return STAGE_DEFINITIONS.find((stage) =>
    stage.aliases.some((alias) => normalized(alias) === candidate),
  );
}

export function findStageData(
  stages: StageSummary[],
  definition: StageDefinition,
): StageSummary | undefined {
  return stages.find((stage) =>
    definition.aliases.some(
      (alias) => normalized(alias) === normalized(stage.key),
    ),
  );
}

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  active: "进行中",
  pending: "等待中",
  ready: "可开始",
  running: "执行中",
  needs_review: "自动复核中",
  blocked: "已阻塞",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  completed: "已完成",
  archived: "已归档",
};

export function statusLabel(status: WorkflowStatus | null | undefined): string {
  if (!status) return "未上报";
  return STATUS_LABELS[status] ?? status;
}

export function statusTone(status: WorkflowStatus | null | undefined): string {
  if (!status) return "unreported";
  if (status === "succeeded") return "success";
  if (status === "active" || status === "running") return "active";
  if (status === "ready") return "ready";
  if (status === "needs_review") return "review";
  if (status === "blocked" || status === "failed") return "danger";
  if (status === "cancelled") return "muted";
  return "pending";
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间格式无效";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function sourceHost(value: string): string {
  try {
    return new URL(value).hostname;
  } catch {
    return "来源地址异常";
  }
}
