# FurnitureWorkflow Agent 试用迭代报告（2026-09-06）

## 结论

本轮交付等级为 **`NOT_READY`**。原因不是代码没有运行，而是附件冻结的共同门槛要求本轮至少 3 个不同站点完成真实网页扫描、类目/数量状态、N=1 Ready 流程；本轮真实访问只得到 Room & Board 的同 Job Sawyer 端到端下载，Interior Define、Alessi、Poly Haven、CGTrader 和 West Elm 均在真实访问层阻断。因此不把旧历史 READY、入口通过或候选 Ready 计作三站端到端，也不推送 GitHub main。

当前代码、可启动网站、真实下载成品和脱敏证据仍然交付，之后重新连通站点即可从持久化检查点恢复。

## 基线与保护

- 仓库：`FurnitureWorkflow-Website-public`，远端仍为原 `origin/main`，未另建产品或平行版本。
- 本轮开始前的未提交成果已保存为脱敏 checkpoint：`d3093c9 Checkpoint real Lux3D trial and website safety fixes`。
- 本轮没有把密钥、Cookie、浏览器 profile、运行数据库、媒体或模型加入 Git。
- 当前分支为 `codex-v3-iteration-20260904`；由于固定试用门槛未满足，本轮只保留本地成果，不更新远端 main。

## 正式进程与网页 UI 证据

当前本机进程已经实际启动并由网页读取：

- Web：`http://127.0.0.1:3000/`
- API：`http://127.0.0.1:8000`
- Website Brain：`CODEX_DEVELOPMENT_BRIDGE`，状态 `READY`
- 视觉输入：`CODEX_BRIDGE_REQUEST_WITH_IMAGE_EVIDENCE`
- L2 Chromium：`READY / ISOLATED_PERSISTENT`
- Blender：`READY`，实际路径为 Blender 5.0.0 Alpha（本机没有 4.5.11）
- Lux3D：诊断为凭据存在，但 Provider 仍受 `OFF_BY_DEFAULT / RECEIPT_REQUIRED` 安全闸门约束；本轮没有新建模 POST

网页操作已实际验证：网站总览显示 Room & Board 类目数量为 `UNKNOWN`，站点详情显示 `PARTIAL`；Job 详情显示生产时间线、`CODEX_DEVELOPMENT_BRIDGE`、候选门 `READY_FOR_MODELING`、`DELIVERY · SUCCEEDED` 和下载入口。失败站点在网页上保留为临时故障，不会把旧类目冒充当前验证结果。

## Sawyer 同 Job 真实闭环

使用已有 Job `job_90bbcd1f29d645b98310ee95009861ff`、候选 `candidate_0798b70642b07e666b14b869` 和 Lux3D task `3304300`。恢复前通过 Website API 写入显式宽度锚点策略，再走正式 resume；没有新建 Job，也没有重复付费。

### 输入和方向

- 产品：Room & Board Sawyer Shower Shelf。
- 产品原图 SHA-256：`f6f57730a2d38756ed9b4f2c39b1e69419cd0e7c2398c61f4721327476cff5cd`。
- 原图和 Blender 四视图真实绑定到 bridge/审核输入；不是手填 PASS。
- 方向：正面 `-Y`、顶部 `+Z`；旋转矩阵行列式 `1.0`；四个视图 hash 均被记录，状态 `CONFIRMED`。

### 尺寸与缩放

官方来源尺寸：`18 × 4 × 3 in`（宽 × 深 × 高）。按文档政策使用官方宽度作为唯一锚点，做整体等比缩放，系数 `0.39859457063258197`，不做逐轴拉伸。

Blender CLI 导入、世界包围盒测量、统一缩放、导出后重新导入复核得到：

| 轴 | 官方 | 导回测量 | 相对误差 |
|---|---:|---:|---:|
| 宽（锚点） | 0.4572 m | 0.4571999907 m | 0.000002% |
| 深 | 0.1016 m | 0.1182212308 m | 16.36% |
| 高 | 0.0762 m | 0.0858359188 m | 12.65% |

锚点宽度通过；非锚点保留真实模型比例偏差并如实记录，不再错误地把三轴 5% 作为默认门。模型 QA 状态为 `PASS` 的含义是方向、矩阵、统一缩放、导出再导入及宽度锚点通过，并不表示深/高被强行拟合到官方数值。

### 下载和哈希

- raw GLB SHA-256：`0bb7baa8411a94ae7299309bc97b3c8ed186de801639615a47efa70f45914221`。
- 归一化交付 GLB SHA-256：`5138e2a8efe33e6e3c34e5b6371d99a7f351a980883446f4ba44cf26828c629e`，4,000,304 bytes。
- Website `DELIVERY_BATCH_ZIP` SHA-256：`156b9f2382960acd363bb98f45e964f8742d0cad776fec4e805fb4af8d37539c`，3,646,766 bytes。
- 通过正式 Website artifact download endpoint 下载并解包，解包 GLB hash 与归一化 GLB 一致。
- Provider ledger 显示已有 task 的 POST=1、POLL=1；本轮 `new_paid_generation=false`，checkpoint reconciliation 事件明确写入“已复用已下载且可验证的原始 GLB；未新增 Provider 请求”。

真实文件位于本机脱敏试用包：

- `closure_evidence_20260906/agent_trial_20260906/downloads/sawyer_website_download.zip`
- `closure_evidence_20260906/agent_trial_20260906/downloads/sawyer_extracted_01/Room & Board Sawyer Shower Shelf__record_0798b70642b07e666b14b869.glb`
- `closure_evidence_20260906/agent_trial_20260906/downloads/visual_input/original/sawyer_source.jpg`
- `closure_evidence_20260906/agent_trial_20260906/downloads/visual_input/blender_orientation/`

## 45 站覆盖

固定 45 站没有改分母。完整逐行结果见 [`trial_evidence/20260906/site_matrix.csv`](trial_evidence/20260906/site_matrix.csv)。本轮统计：

- 固定分母：45
- 本轮启动：6 个不同站点、7 次 scan run（另含 West Elm 的 offline guard）
- 扫描完成：7 次 run（5 个 `TEMPORARY_FAILURE`、1 个 `PARTIAL`、1 个 `LIVE_SCAN_REQUIRED`）
- 本轮 `READY_FOR_MODELING` 流程：1 个候选（Sawyer）
- 真实网页下载：1 个 Job / 1 个 GLB
- 本轮 0 个站点达到完整“扫描+类目/数量+N=1 Ready+下载”之外的额外站点门槛；其余固定站保留 `NOT_RUN`，没有从分母删除

访问层结果是实际阻断：Alessi、Interior Define、Poly Haven 的 HTTP 阶段由 Website 新增的有界超时收为 `SITE_SCAN_TIMEOUT`；CGTrader 的浏览器阶段同样有界超时；West Elm 的 `live=false` 请求被正确拒绝为 `LIVE_SCAN_REQUIRED`，`live=true` 后保留 `BROWSER_NAVIGATION_FAILED`。这些不是 fixture，也没有手工写入类目或数量。

## 本轮修复

1. **显式尺寸锚点契约**：Job policy、Control API、生产运行契约和 Blender QA 都传递 `dimension_anchor_axis` 与 `allow_non_anchor_dimension_error`；默认严格模式仍保留。
2. **统一缩放而非逐轴拉伸**：显式宽度锚点只计算一个比例因子，最终闸门只放宽非锚点误差，不放宽错误方向、镜像、剪切、无效 GLB 或错误产品。
3. **真实 Blender CLI 闭环**：加入父子 mesh 世界包围盒、方向矩阵、导出后重新导入和多视图 hash 证据，拒绝 fake/mock Blender 冒充。
4. **同 Job 下载检查点恢复**：对已有 provider task 和可验证 raw GLB，在 `MODEL_DIMENSION_CONFLICT`、`BLENDER_QA_FAILED`、`DOWNLOAD_FAILED` 等终态复用 raw，转入正式 Blender/交付路径，不再次 POST。
5. **扫描 worker 有界超时**：HTTP/L2 analyzer 均运行在 daemon worker 并受 `WEBSITE_SITE_SCAN_TIMEOUT_SECONDS`（默认 120 秒，试用 launcher 使用 60 秒）约束；超时持久化为 `TEMPORARY_FAILURE/SITE_SCAN_TIMEOUT`，保留同一浏览器会话和检查点，迟到结果不写入数据库。
6. **启动器白名单**：`WEBSITE_SITE_SCAN_TIMEOUT_SECONDS` 加入原 `launch_website.py` 的私有配置白名单。

## 回归检查

- Python 全套：`services/api/tests`、`packages/workflow_core/tests`、`packages/workflow-engine/tests` 全部通过。
- 编译与差异：`compileall`、`git diff --check` 通过。
- Web：`npm run typecheck` 通过；`npm run lint` 0 errors、2 个既有 warning（未使用 `useRef`、`<img>` LCP 建议）；`npm run build` 通过。
- 站点扫描超时新增回归测试验证了持久化状态、错误码、浏览器会话状态和可恢复性。

## 未完成与实际影响

- `COMPANY_MODEL_NOT_TESTED`：公司 Brain/Vision API 本轮没有实连，不能声称与 bridge 判断等价；本地已验证请求结构和图像证据字段。
- 只有 Sawyer 走过一次真实 Lux3D/Blender/Website download；没有为凑三站而追加计费任务。
- 45 站尚未测完；外部站点访问阻断和预算策略需之后继续恢复，不能据此推导全站成功率。
- Provider 在系统页仍显示 `OFF_BY_DEFAULT`，这是安全闸门的真实状态，不是失败伪装。

## GitHub 状态

本轮没有 push。原因是固定试用条件（3 个不同站点的真实 Ready）未满足；保留本地 checkpoint 与本轮代码，origin/main 历史不变。达到门槛并完成同样回归后，才按原授权普通合并/推送，不 force push。

本轮本地收口提交：`Complete Codex bridge trial hardening and evidence`（具体 SHA 以交付时 `git log -1` 为准，当前工作树干净）；远端 `origin/main` 未改变。
