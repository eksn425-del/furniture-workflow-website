# 本机试用说明：Website + Codex Development Bridge

这份说明对应 2026-09-06 试用代码。Website 执行网页扫描、类目/数量证据、候选筛选、Lux3D/Blender/下载和持久化；Codex 只在 `CODEX_DEVELOPMENT_BRIDGE` 中读取 Website 发来的证据并返回判断或工具调用。桥接脚本不会自动推理，也不会预填 PASS。

## 1. 启动 Website

先在项目根目录打开 PowerShell。以下变量只指向本机目录，不包含密钥；Lux3D、浏览器和 Blender 的私有配置仍放在被 Git 忽略的 `.env.local` 或进程环境中：

```powershell
Set-Location E:\living\FurnitureWorkflow-Website-public
$env:WEBSITE_MODEL_MODE = "CODEX_DEVELOPMENT_BRIDGE"
$env:CODEX_BRIDGE_ROOT = "E:\living\closure_evidence_20260906\agent_trial_20260906\bridge"
$env:OUTPUT_ROOT = "E:\living\closure_evidence_20260906\website_real"
$env:WEBSITE_SITE_SCAN_TIMEOUT_SECONDS = "60"
python .\launch_website.py
```

启动器会启动 API `http://127.0.0.1:8000`、Web `http://127.0.0.1:3000`，并等待两者健康。端口已被占用时不要重复启动；先关闭上一份启动器。按 `Ctrl+C` 停止两个子进程。启动器只转发显式白名单配置，不打印密钥。

如果只是接手已经运行的本轮进程，可直接打开 `http://127.0.0.1:3000/`；系统页应显示 `Website Brain READY / CODEX_DEVELOPMENT_BRIDGE`、`视觉输入 CODEX_BRIDGE_REQUEST_WITH_IMAGE_EVIDENCE`、`Blender READY` 和 `Provider OFF_BY_DEFAULT`。

## 2. Codex 伴随响应循环

保持 Website 进程和当前 Codex 会话都运行。Website 需要大脑时会把真实请求写入：

```text
E:\living\closure_evidence_20260906\agent_trial_20260906\bridge\pending\<request_id>.json
```

请求包含当前阶段、网页执行结果、产品原图/视图的路径或 hash、候选证据和上一轮状态。Codex/操作者应先读取这个 JSON，必要时实际查看指定图片或 Blender 多视图，再写出真实判断；不要因为请求存在就返回 PASS。

响应必须由现有包装器原子写入 `responses`：

```powershell
python .\scripts\codex_bridge_respond.py `
  --root "E:\living\closure_evidence_20260906\agent_trial_20260906\bridge" `
  --request-id "<request_id>" `
  --message-json '{"role":"assistant","content":{"decision":"...","reason_codes":["..."]}}' `
  --metadata-json '{"source":"codex_session","evidence_reviewed":true}'
```

只填写你实际审过的证据；若证据不足，应返回拒绝、需要复核或具体工具调用。Website 会轮询同名响应并继续持久化事件。请求等待或失败时，网页应显示当前阶段、reason code 和可恢复操作；不要改数据库或手写完成状态。

## 3. 网页操作路径

1. “网站总览” → “添加并摸底”或从固定 45 站选择入口。
2. 仅在扫描返回真实类目和数量状态后选择类目；`UNKNOWN` 不得当作 Exact 数量。
3. 新建 Job，选择 `EXACT_N=1`，等待候选进入 `READY_FOR_MODELING`；`Ready Pool` 或入口通过不等于下载成功。
4. 生产页确认原图绑定、视觉/类目审核、官方尺寸来源、方向视图和 Provider ledger，再开始受闸门保护的生产。
5. 交付页只下载 `DELIVERED` artifact。raw GLB 与归一化交付 GLB 必须用 hash 区分。

本轮已验证的真实成品是 Room & Board Sawyer，详见 [`FurnitureWorkflow_Agent_Trial_Report_20260906.md`](FurnitureWorkflow_Agent_Trial_Report_20260906.md)。

## 4. 公司 API 模式（待环境验证）

公司环境可在私有配置中选择 `MULTIMODAL_SINGLE_MODEL`，或在同时配置独立视觉端点时选择 `TEXT_BRAIN_PLUS_VISION`，并提供对应的 `WEBSITE_BRAIN_*` / `WEBSITE_VISION_*`（以及已有 `LUX3D_*`）变量。不要把值写入 Git 或聊天。本轮没有实连公司 Brain/Vision，状态明确为 `COMPANY_MODEL_NOT_TESTED`；接通后至少复测同一类目取证、同一产品图判断和同一 Blender 多视图判断，优先复用已有 Sawyer 资产，不新增 Lux3D 费用。

## 5. 访问阻断和恢复

扫描超时会记录为 `TEMPORARY_FAILURE/SITE_SCAN_TIMEOUT`；浏览器导航失败会保留同一 `browser_session_id`。在网站详情点击“恢复同一扫描”，或通过原恢复入口重试；不要新建第二个 Job 来绕过检查点。每次重试仍必须是真实 `live=true`，离线请求会被 `LIVE_SCAN_REQUIRED` 拒绝。

固定 45 站结果保存在 [`trial_evidence/20260906/site_matrix.csv`](trial_evidence/20260906/site_matrix.csv)。`NOT_RUN`、`ACCESS_BLOCKED`、`READY_ONLY`、`END_TO_END_PASS` 等状态均保留，不能从分母删除失败站点。
