# FurnitureWorkflow Lux3D 真实闭环报告（2026-09-06）

## 结论先行

本轮已把运行中的 Website API/worker 接到 Codex development bridge、真实 Lux3D client 和本机 Blender 4.5.11；C01–C12 的代码缺陷均已复现、修复或建立了失败闭环，Python/前端自动门通过。真实 Room & Board 样本完成了：公开候选 → 图片复核 → 1 次真实 Lux3D 创建 → 查询 → GLB 下载 → Blender 四视图 → Codex 方向判断。

但本轮不能诚实地宣布“可交付模型完成”：该真实 GLB 的三轴官方尺寸与模型比例冲突，严格等比缩放无法在 5% 几何公差内同时满足，系统因此保留 raw GLB 并阻断规范化交付；没有网页下载成品，也没有再次付费重试。Interior Define 的不计费 D 级作业也因真实商品页临时故障只完成 1/3 官方尺寸 Ready Pool。按执行文档，G4、G5、G7、G8 尚未通过，所以没有合并或推送 `main`。

## 基线、代码和运行状态

| 项目 | 实际值 |
|---|---|
| 工作树 | `E:\living\FurnitureWorkflow-Website-public` |
| 分支 | `codex-v3-iteration-20260904` |
| 当前 HEAD | `03c698c94b72b96c1d38926a327b9c7bb0a45ffd` |
| `origin/main` | `7d6e098e9623f907df828b05d7a00cbad2ba4821` |
| remote | `https://github.com/eksn425-del/furniture-workflow-website.git` |
| 状态 | 保留最新工作树改动，未 commit/push；没有 force push 或改写历史 |
| API/UI | `http://127.0.0.1:8000` / `http://127.0.0.1:3000` |

当前进程 `/api/v1/control/system` 实测：Website Brain `READY / CODEX_DEVELOPMENT_BRIDGE`，Lux3D `READY / CREDENTIALS_PRESENT`，Blender `READY`，L2 browser `READY`，production worker `READY`。配置中的密钥、Cookie 和签名下载地址未写入本报告或证据包。

Lux3D 使用项目现有 client 和生产协议；脱敏后的服务主机为 `api.aholo3d.cn`，模型标识为 `v3.0-standard`。本轮采用项目已有更严格的本 Job 审批上限：允许创建 1 个、金额上限 1000 minor；实际只创建 1 个任务。整轮没有第 2/3 次创建，也没有因 unknown 盲目重发。

## C01–C12 处理表

| 缺陷 | 本轮处理和证据 | 结论 |
|---|---|---|
| C01 方向识别缺失 | production pipeline 在 raw GLB 下载后调用真实 Blender 渲染 front/top/right/iso，再把参考图 hash、四视图 hash、raw GLB hash 和 candidate 绑定到 bridge 判断；真实 Sawyer 样本得到 `front=-Y, top=+Z`。未确认仍走 `ORIENTATION_REVIEW_REQUIRED`。 | 代码/真实方向链通过；样本后续因尺寸冲突未交付 |
| C02 矩阵只看 det | `workers/blender_adapter.py` 与 Blender worker 同时校验 3×3、有限值、正交误差和 det≈+1；非等比、剪切、镜像、NaN/Inf、错误维度均拒绝。 | 通过；自动回归通过 |
| C03 父子 mesh 重复变换 | Blender worker 先快照所有 world matrix，再统一应用目标变换；真实 Blender 跑 nested parent、shared mesh instances、source-front-plus-X 三组，导回 QA 均 `PASS`。 | 通过 |
| C04 取整尺寸贯通 | `_stage_dimension` 保留归一化精确值，展示整数与模型目标分离；缺单位/官方尺寸不再静默当英寸，L2 临时失败保持可恢复。真实 GLB 的模型比例冲突被 `MODEL_DIMENSION_CONFLICT` 阻断，未用非等比拉伸。 | 保护逻辑通过；真实样本正确阻断 |
| C05 命名消歧碰撞 | 命名保留完整身份摘要、≤50 字、类型词表；旧 checkpoint 在未提交前按 `deterministic-product-name.v2` 重建。真实名称含 `Sawyer Shower Shelf`、`Filmore Towel Bar`、`Slim Towel Bar with Shelf`。 | 通过 |
| C06 图片优选不足 | 生产路径保存拒绝 URL/hash，下载失败后只尝试新候选；身份绑定的结构化媒体可标 `COMPATIBLE`，真正冲突仍拒绝。真实 Interior Define 9 张图片逐张经过 bridge，未使用预填答案。 | 通过代码/真实图片层 |
| C07 profile 整体 VALIDATED 卡住能力 | profile capability 以单项能力和证据版本复用，漂移只重学受影响能力；相关 Domain Agent 回归通过。 | 通过 |
| C08 正式进程未接线 | API/worker 的有效环境实际显示 bridge、Blender 版本与 Lux3D READY；不是只在临时 shell 展示。 | 通过 |
| C09 D/E 定义混乱 | `run_45_site_acceptance.py` 固定 D 目标为 3，1–2 件不会被改标 PASS；`image_decodable` 与视觉通过分开；E 单独记录真实 Provider。 | 代码通过；真实 D 仍 PARTIAL |
| C10 矩阵与最终代码不对应 | 在当前工作树上重新跑 45 站 A；代表站使用正式 runtime/factory、同一 bridge、checkpoint 和真实网页。 | A 完整；B–D 代表站部分仍受外部/页面故障影响 |
| C11 导回一致不等于正确 | QA 同时记录方向来源、变换合法性、部件关系、精确尺寸误差、导回和多视图；raw GLB 永久保留，尺寸冲突不交付。 | 通过安全门；真实样本未达交付 |
| C12 证据不可独立复核 | 本报告、证据索引、45 站 JSON、bridge 脱敏摘要、Blender report、raw GLB hash 和四视图均保留；未打包密钥/数据库/完整日志。 | 通过 |

## 真实 Lux3D E 测试

作业：`job_90bbcd1f29d645b98310ee95009861ff`（Room & Board Bath Hardware，Exact N=1）。

- 真实候选：`candidate_0798b70642b07e666b14b869`（Sawyer Shower Shelf），官方尺寸 18×4×3 in。
- 官方尺寸证据：Room & Board 产品页 `https://www.roomandboard.com/catalog/bath/bath-hardware/sawyer-shower-shelves` 的页面文本明确写为 `Sawyer 18w 4d 3h Shower Shelf`；抓取字段为 `width=18.0`、`depth=4.0`、`height=3.0`、`unit=in`，换算为 `457.2×101.6×76.2 mm`。`dimension_status=READY`、`dimension_lookup_state=OFFICIAL_FOUND`，不是估算值。
- 网站实际下载的原图：`02_qualification/media/candidate_0798b70642b07e666b14b869__media_0.jpg`，SHA-256 `f6f57730a2d38756ed9b4f2c39b1e69419cd0e7c2398c61f4721327476cff5cd`；该文件就是本次 Lux3D 输入图。
- 真实 Provider task：`3304300`。
- 创建 POST：1；可计费尝试：1；unknown 创建：0；查询：1；重复创建：0。
- Provider 返回成功并下载 raw GLB；下载文件 4,338,580 bytes，SHA-256 `0bb7baa8411a94ae7299309bc97b3c8ed186de801639615a47efa70f45914221`。
- Blender 4.5.11 真实导入和四视图 report：`status=PASS`；Codex 实际查看 front/top/right/iso 后确认 `front=-Y`、`top=+Z`，四张 PNG 的 hash 已写入 candidate receipt。
- raw bbox（Blender units）：W `1.1470301151`，D `0.2965951860`，H `0.2153464407`。
- 精确目标（模型单位）：W `0.4572`，D `0.1016`，H `0.0762`。唯一等比缩放因子 `0.3649991563` 后相对误差分别为 8.43%、6.55%、3.15%；宽/深超过 5% 几何公差。非等比变形被生产契约禁止，所以状态为 `MODEL_DIMENSION_CONFLICT / REVIEW_REQUIRED`。
- 结果：raw GLB、四视图和冲突证据保留；没有 normalized GLB、delivery bundle 或网页“成功下载”记录。后续应在同一 Job 补充尺寸/变体核对，或把该产品标为人工复核；不能重发同一付费任务。

证据：

- [真实 Job 脱敏接口结果](E:/living/closure_evidence_20260906/website_real/web_productions/roomandboard.com/job_90bbcd1f29d645b98310ee95009861ff)
- [Blender 尺寸冲突证据](E:/living/closure_evidence_20260906/website_real/web_productions/roomandboard.com/job_90bbcd1f29d645b98310ee95009861ff/04_provider/glb/blender_dimension_conflict.evidence.json)
- [raw GLB（仅证据，非最终交付）](E:/living/closure_evidence_20260906/website_real/web_productions/roomandboard.com/job_90bbcd1f29d645b98310ee95009861ff/04_provider/glb/Room%20%26%20Board%20Sawyer%20Shower%20Shelf__record_0798b70642b07e666b14b869.glb)
- [四视图 report](E:/living/closure_evidence_20260906/website_real/web_productions/roomandboard.com/job_90bbcd1f29d645b98310ee95009861ff/04_provider/glb/orientation_views/record_0798b70642b07e666b14b869/orientation-views.json)

## G5 真实模型交付门（你提到的“5G”）

这里的“5G”按执行文档中的 `G5` 理解：真实 Provider 生成的模型必须经过真实 Blender 导入、方向确认、父子/矩阵检查、官方精确尺寸的等比缩放与复核，最后才允许形成网页可下载交付物。

- 真实 Provider 创建已发生且可追溯：task `3304300`，创建 1 次、计费 1 次、unknown 0 次，未重复付费重试。
- 方向门通过：真实 Blender 4.5.11 四视图 `front/top/right/iso`，Codex 复核为正面朝 `-Y`、顶部朝 `+Z`。
- 精确尺寸门未通过：raw 模型包围盒为 `1.1470301151×0.2965951860×0.2153464407` Blender units；按官方 `18×4×3 in` 统一缩放后，宽/深/高误差为 `8.43%/6.55%/3.15%`，宽和深超过 5% 公差。
- 处理结论：状态 `MODEL_DIMENSION_CONFLICT / REVIEW_REQUIRED`。禁止非等比拉伸，因此只保留 raw GLB 和证据视图，不生成或宣称 normalized GLB、delivery bundle 或网页成功下载。这个 G5 结果是“真实执行但未通过交付门”，不是“尺寸缺失”。

## 不计费 D 级真实采集

Interior Define 的正式公开 Magento PWA 类目 `/bedroom` 使用 Provider OFF、Exact N=3。真实采集发现 9 个候选；9 个本地图片均真实下载并逐张送入 bridge 视觉判断，身份和图片 hash 保留。一个候选含官方结构化尺寸并进入 `DIMENSION_READY`，其余候选因公开 PDP 显示临时技术故障进入 `DIMENSION_PENDING`，作业以 `TEMPORARY_PAGE_FAILURE` 可恢复阻断结束；Provider 调用为 0。目标 3 没有被降成 1，因此 D 结果是 PARTIAL，不是假 PASS。

证据目录：

- [D 作业摘要](E:/living/closure_evidence_20260906/acceptance_interiordefine_direct_d/direct_d_run.json)
- [D 候选池](E:/living/closure_evidence_20260906/acceptance_interiordefine_direct_d/candidate_pool.json)
- [D 事件链](E:/living/closure_evidence_20260906/acceptance_interiordefine_direct_d/candidate_pool_events.jsonl)

## 45 站最终 A 矩阵

最终 A 运行：`acceptance_20260905T192959Z`，总分母 45，当前代码实际结果：

| 结果 | 数量 | 说明 |
|---|---:|---|
| PASS | 30 | 真实入口可访问，未把它们自动宣称 B/C/D 完成 |
| ACCESS_BLOCKED | 9 | robots 或需可见浏览器会话：Anthropologie、Arhaus、Ligne Roset、RH、GrabCAD、Free3D、3DExport、Safavieh、Driade |
| ENVIRONMENT_BLOCKED | 1 | Iris bounded timeout（35s），保留 checkpoint/归因 |
| FAILED | 5 | Interio、Archive3D、Archibase、NASA 3D、3DXO 的公开入口 preflight failed |

完整逐站 JSON：[acceptance_acceptance_20260905T192959Z.json](E:/living/closure_evidence_20260906/acceptance45_layer_a/acceptance_acceptance_20260905T192959Z.json)。

代表站 B–D（此前正式 runner，未伪造结果）：Interior Define 的 B PASS、C PASS（发现 3），旧 D 因当时 bridge 进程接线问题未运行；本轮同一站点已用真实 bridge 完成不计费 D 并得 PARTIAL。Room & Board B PARTIAL、C 为 UNSUPPORTED_CAPABILITY；CGTrader B 为有界超时需修复；West Elm B 为临时浏览器失败。它们分别覆盖 Magento PWA、generic direct-brand、marketplace、临时浏览器故障四种结构/故障边界，没有把失败归因成“站点不支持”。

## 恢复、取消和重复提交安全

Room & Board 同一 Job 曾经历初次 bounded tick、两次恢复和最终 Provider 运行；Provider ledger 只出现 task `3304300`，没有重复 task。D 作业的图片、候选池和尺寸阻断均写入 durable checkpoint，可从同一 Job 补证据。未对真实 Lux3D 制造付费超时；unknown 任务按禁止重提处理。

## 自动化回归与前端门

- `pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests`：exit 0，完整测试集通过。
- `python -m compileall -q services/api/app packages/workflow_core packages/workflow-engine/src workers`：exit 0。
- `apps/web`: `npm run typecheck` exit 0；`npm run lint` exit 0（2 个既有 warning，无 error）；`npm run build` exit 0。
- `git diff --check` exit 0；脱敏凭据模式扫描无命中。
- 真实 Blender 反例三组（nested parent、shared mesh instances、source-front-plus-X）均生成真实 normalized GLB、前后多视图和 QA `PASS`，证据在 `E:\living\closure_evidence_20260906\blender_real`。

## 公司接入步骤（不含密钥）

1. 在运行 Website API/worker 的同一进程环境设置已有 `WEBSITE_BRAIN_*` 公司协议变量，确认公司 endpoint 实际采用当前 client 支持的 `/chat/completions` 兼容协议；不要把公司 Brain 与 Lux3D Provider 混为一层。
2. 生产环境将 `WEBSITE_MODEL_MODE` 切到公司已批准的模式，并保留 `CODEX_DEVELOPMENT_BRIDGE` 作为本地回归模式；启动后访问 `/api/v1/control/system`，确认 `website_brain.status=READY`、`review_provider` 与预期一致。
3. 在同一 worker 进程配置 `BLENDER_WORKER_ENABLED=true` 与可执行文件路径；诊断必须显示 Blender `READY` 和版本。
4. 在同一 worker 配置 Lux3D 的已有变量和预算/并发限制；先做非创建式查询，再由 Website UI 的 `PRODUCTION_READY` 审批门提交。不要把 key 写进仓库、报告或聊天。
5. 用 1 个不计费 Ready Pool 候选做公司 Brain 同样本回归；对照 bridge receipt 的图片 hash、尺寸来源、方向 receipt 和错误码，只有同一证据链稳定后再扩大批量。

## 验收门与 GitHub 状态

| 门 | 本轮结论 |
|---|---|
| G1 | PARTIAL：代码工作树包含最新修复，但尚未提交 |
| G2 | PASS：运行中的 API/worker 已接 bridge、Blender、Lux3D 配置 |
| G3 | PARTIAL：几何反例回归通过；真实 Sawyer 按契约正确拒绝比例冲突 |
| G4 | PARTIAL：Interior Define Ready Pool 1/3，官方尺寸页临时失败 |
| G5 | NOT PASS：真实 task/GLB/方向完成，但精确尺寸冲突，未生成网页可下载交付 |
| G6 | PASS（恢复安全）：同 Job 恢复不重复 Lux3D；D checkpoint 可恢复 |
| G7 | PARTIAL：45 站 A 完整；代表站 B–D 如实保留外部/代码阻断 |
| G8 | NOT COMPLETE：没有可宣称的最终网页模型下载 |
| G9 | PASS：Python、前端、编译、差异和脱敏检查通过 |

因此没有执行 commit、merge 或 push。满足 G4/G5/G8 后，应在当前分支先提交并回归，再按仓库实际保护规则 fast-forward/合并到原 `main`；当前 `origin/main` 未改变。

## 本轮后续最小动作

不需要新的付费任务：在同一 Room & Board Job 补查 Sawyer 的变体/单位证据，或人工标记其比例冲突；在 Interior Define 同一 D Job 通过持久浏览器会话补齐至少 3 个官方尺寸候选。两者完成后再从网站原入口重跑 Blender/下载门，仍不得修改历史 task 或重提 unknown。
