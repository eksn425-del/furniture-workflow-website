# FurnitureWorkflow Production Closure 报告

日期：2026-09-06（Asia/Shanghai）  
基线：最新 Final 本地源码，原分支 `codex-v3-iteration-20260904`，起始提交 `1274554f61e24b14319341a73bc800f79774de76`。  
远端：`https://github.com/eksn425-del/furniture-workflow-website.git`；本轮读取到的 `origin/main` 为 `7d6e098e9623f907df828b05d7a00cbad2ba4821`。  
执行方式：真实 Python/Next.js 测试、真实 Edge/Chromium 员工 UI、真实 Blender 4.5.11 CLI、真实公开网页和本地 Codex bridge；没有用 mock Blender、预填 PASS、固定答案或网站外隐形救援。

## 结论

本轮已经把 R01–R09、R13–R14 的本地工程逻辑和证据链补齐，回归套件与前端构建均通过；真实 Blender 批次 13 个结构 fixture 中 12 个通过，1 个倾斜语义模型被正确阻断。图片解码/视觉拒绝换图、命名词表/唯一性、尺寸单位和 bridge 重启恢复均有实际证据。

生产闭环目前**未达到允许更新 main 的验收门**：

- 45 站完整矩阵已经实际运行并保留结果，但 B/C 仍有代码缺陷、环境阻断和 partial；D 真实员工生产操作没有形成可交付 GLB。
- 正式 API/runtime 的 C 路径已接入生产 `ProductionPipeline` 和 agent recovery 回调；最新 Durian 复测在公开站点发现阶段真实超时，留下 durable checkpoint，不能伪报为 PASS。
- 员工 UI 的真实扫描、任务状态和错误显示通过；生产入口在未选类目时真实返回 HTTP 400，delivery 页面没有伪造下载。
- 公司内部 API、正式 vision/agent runtime、员工生产 GLB/download 尚未实连或未闭环。因此本轮**不 push、不覆盖远端 main**；远端 SHA 未改变。

本地改动已保留，后续可在补齐公司凭据、人工选择类目并完成三技术结构 D 闭环后继续验收。

## 本轮实现内容

1. `workers/blender_adapter.py`：gltf/glb world-space bbox、父级 TRS/quaternion/matrix、active scene roots、shared mesh、显式单位未知阻断、正确 det+1 orientation rotation、uniform scale 规划、导出后重导入 QA。
2. `workers/blender_normalize_qa.py`：真实 Blender CLI 导入、世界尺寸、正面/顶部旋转、地面锚定、整体等比缩放、Workbench 多视图、glb 导出和 clean re-import。
3. `scripts/blender_closure_fixture.py`：只用于生成可追踪的结构测试 fixture；报告中的 fixture 明确标为 synthetic，不冒充供应商模型。
4. `workers/production_pipeline.py`：真实图片 bytes/hash/decode/pixel/format/channel 检查；同一产品图库内拒绝后换图；视觉质量、完整性、重复图和 receipt/hash 校验；官方尺寸/partial/unknown 状态和 anchor policy。
5. `packages/workflow_core/naming.py` / `naming_vocabulary.json`：正式 style/color/material/type 词表、review-required、稳定身份后缀、50 字符上限和 `Natural` 颜色规范。
6. `services/api/app/services/brain_provider.py`：视觉字段、保守合并、`BrainCancelled`，文件型 bridge v2（logical session、request/turn、input hash、原子写、短锁、重启复用、旧响应忽略、取消标记、超时）。
7. `services/api/app/services/product_acquisition.py`：in/cm/mm/m、引号单位、末尾全局单位、混合单位解析；L2 批次总预算；同一产品恢复和不借用其他 SKU 图片。
8. `services/api/app/services/site_profile.py` / `site_scan_runtime.py`：可复用 capability/status 和持久化扫描状态。
9. `scripts/run_45_site_acceptance.py`：正式 runtime 分类、durable receipt/checkpoint/poll/resume、Windows 隔离子进程、BROWSER_REQUIRED、超时/网络错误分类、C 使用生产 acquisition 工厂和真实 agent recovery callback。

## 自动化回归和构建

使用的环境变量：

```text
PYTHONPATH=E:\living\FurnitureWorkflow-Website-public;
E:\living\FurnitureWorkflow-Website-public\services\api;
E:\living\FurnitureWorkflow-Website-public\packages\workflow-engine\src;
E:\living\FurnitureWorkflow-Website-public\packages
```

通过项目 Python 回归：

```text
python -m pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests
=> [100%], exit 0
```

重点子集也通过：`test_native_site_analysis.py`（26 tests）、`test_product_acquisition.py` 与 `test_production_convergence.py`；`compileall` 通过，`git diff --check` 通过。

前端（`apps/web`）：

- `npm run lint`：exit 0，仅保留既有两条 warning（unused `useRef`、`<img>` 规则）。
- `npm run typecheck`：exit 0。
- `npm run build`：exit 0，Next 16.3.0 webpack，静态路由构建完成。

## 批次一：真实 Blender 世界尺寸和方向

Blender 实例：`E:\living\tools\blender-4.5.11\blender-4.5.11-windows-x64\blender.exe`，`Blender 4.5.11 LTS`，build 2026-06-23，hash `4db51e9d1e1e`。

证据目录：`E:\living\closure_evidence_20260905\blender`。

- `batch_results.json`：13 个 fixture；`PASS=12`，`BLOCKED_WITH_EVIDENCE=1`。
- 通过案例覆盖 width>depth、depth>width、近似相等、source front +X/+Y、top -Z、父级缩放、嵌套父级旋转、shared mesh instance、分离多 mesh、对称无唯一正面、较宽产品。
- 每个通过案例均有 raw bbox、rotation matrix、scale factor、final bbox、reimport bbox、SHA256 和 Blender QA JSON；正面合同为 `-Y`，顶部合同为 `+Z`，旋转 determinant=1。
- `tilted_unsupported` 明确返回 `BLOCKED_WITH_EVIDENCE`，不会按最近轴强行 PASS。
- idempotence：`E:\living\closure_evidence_20260905\blender\normalized\idempotence_result.json` 为 PASS；第二次规范化没有累积变换。
- 边界：`edge_cases.json` 真实验证 UNKNOWN_UNIT、uniform conflict、empty model、malformed glb；均按可诊断状态处理。
- 实际 Workbench 多视图已输出 raw/final front、top、right、iso，例如 `normalized\source_front_plus_x.normalized.views\final_iso.png`。这是 synthetic 结构证据，不是供应商家具效果宣传图。

尺寸政策说明：fixture 的 target 与重导入误差均被记录，常规样例误差约 0.003–0.047 m，当前结构 QA 以真实 Blender import/export、方向、等比关系、地面锚定和重导入一致性为门；精确供应商尺寸仍须进入员工正式 D 流程后用官方尺寸覆盖。

## 批次二：图片、命名、尺寸

证据总表：`E:\living\closure_evidence_20260905\batch2_evidence.json`。

- 真实公开 sofa JPEG：`E:\living\closure_evidence_20260905\media\real_public_sofa.jpg`，111452 bytes，SHA256 `fa7676f6b37a6823bb97dc11b5dac7fae718a0a28d15c828446985170000abe0`；已实际查看并用 bridge 返回 single/complete/simple/background、confidence 0.94。
- 视觉失败换图：同一身份第一张 `VISUAL_NOT_SINGLE_PRODUCT` 被拒绝，第二张 gallery image 被接受；`result.json` 与 `result_after_second_review.json` 均记录 rejected URL、selected URL、bytes/hash。
- 图片 decode 记录 JPEG、RGB、1400×933、channels=3、无 alpha、低变化通道为 0；坏图和不可解码内容会被拒绝。
- 命名：`Alien`、`Electric Purple`、`Foam`、`Alien Thing` 均为 `REVIEW_REQUIRED`；官方名 `Room & Board Metro Chair Modern Natural Wood`；重复输入加 `V-ABC-123/124` 后稳定且 ≤50 字符。
- 尺寸：完整 cm、混合 cm/in、partial official、unknown lookup 四种状态均有证据；unknown 不补造尺寸，partial 不冒充完整。

## 批次三：正式 runtime、45 站、恢复和下载

### 45 站完整矩阵

完整 JSON/CSV（45 行、每站 A/B/C/D、receipt/checkpoint 路径）在：

- `E:\living\closure_evidence_20260905\acceptance_formal_45_bc_v10\acceptance_acceptance_20260905T150520Z.json`
- `E:\living\closure_evidence_20260905\acceptance_formal_45_bc_v10\acceptance_acceptance_20260905T150520Z.csv`

以下为该次完整运行的逐阶段分组，合计每阶段 45 站：

**A — preflight / robots / reachability**

- `PASS`（29）：Article、Castlery、Interior Define、MacKenzie-Childs、Nathan James、POLYWOOD、Room & Board、Rove Concepts、Sixpenny、Walker Edison、India Art n Design、Fabuliv、Globally Indian、Indikasa、Hometown、Featherlite、Durian、Indian Nest、Furnishka、LoveNSpire、Indian Hub、DesignConnected、CGTrader、3D Warehouse、Sweet Home 3D、MyMiniFactory、Poly Haven、Alessi、West Elm。
- `ACCESS_BLOCKED`（9，robots/访问策略）：Anthropologie、Arhaus、Ligne Roset、RH、GrabCAD、Free3D、3DExport、Safavieh、Driade。
- `FAILED`（7）：Kayu、Iris、Interio、Archive3D、Archibase、NASA 3D、3DXO。

**B — bounded L2 collection**

- `PASS`（4）：Sixpenny、Durian、LoveNSpire、Sweet Home 3D。
- `PARTIAL`（9）：Article、MacKenzie-Childs、Nathan James、POLYWOOD、Indian Hub、DesignConnected、MyMiniFactory、Poly Haven、Alessi。
- `ENVIRONMENT_BLOCKED`（6）：Interior Define、Walker Edison、India Art n Design、Globally Indian、Indikasa、West Elm。
- `CODE_DEFECT`（9）：Castlery、Room & Board、Rove Concepts、Fabuliv、Hometown、Featherlite、Indian Nest、Furnishka、CGTrader。
- `UNSUPPORTED_CAPABILITY`（1）：3D Warehouse。
- `NOT_RUN`（16）：Anthropologie、Arhaus、Ligne Roset、RH、Kayu、Iris、Interio、Archive3D、GrabCAD、Free3D、Archibase、3DExport、NASA 3D、3DXO、Safavieh、Driade。

**C — bounded product discovery / pagination**

- `PASS`（1）：DesignConnected。
- `PARTIAL`（1）：Sweet Home 3D。
- `UNSUPPORTED_CAPABILITY`（2）：Article、MyMiniFactory。
- `CODE_DEFECT`（9）：MacKenzie-Childs、Nathan James、POLYWOOD、Sixpenny、Durian、LoveNSpire、Indian Hub、Poly Haven、Alessi。
- `NOT_RUN`（32）：Anthropologie、Arhaus、Castlery、Interior Define、Ligne Roset、RH、Room & Board、Rove Concepts、Walker Edison、India Art n Design、Kayu、Fabuliv、Globally Indian、Indikasa、Iris、Hometown、Featherlite、Interio、Indian Nest、Furnishka、Archive3D、GrabCAD、CGTrader、Free3D、Archibase、3DExport、3D Warehouse、NASA 3D、3DXO、Safavieh、Driade、West Elm。

**D — employee production / real model / download**

- 当前完整矩阵中 45 站均 `NOT_RUN`，因为前置 B/C 没有达到生产门；没有伪造 GLB、交付批次或下载链接。

该 v10 矩阵是在最终错误类型传播和 C 工厂回接补丁之前的完整基线。最终补丁后做了 focused 复测：

- `E:\living\closure_evidence_20260905\acceptance_formal_durian_v11`：Durian B PASS；C 对 SSL EOF 如实分类为 `ENVIRONMENT_BLOCKED / NETWORK_ERROR`。
- `E:\living\closure_evidence_20260906\acceptance_formal_durian_v12`：Durian B PASS；C 已走生产 `ProductionPipeline` acquisition factory 和 agent recovery callback，公开产品发现真实超过 180 秒，返回 `CODE_DEFECT / TIMEOUT_REQUIRES_DIAGNOSIS`，保留 `products\Durian\acquisition_checkpoint.json` 与 production.db；没有生成假产品。

### 员工真实 UI/API

证据：`E:\living\closure_evidence_20260905\employee_api\ui_employee_evidence.json`。

- 真实 Edge/Chromium 访问本地网站；创建新 job，真实扫描公开 `https://www.interiordefine.com/`。
- Job：`job_53f4c566edbd4809a3530e0d2843c251`；scan：`scan_9348c0f58aab40a68b9c6057e38de50a`。
- UI 显示 `TAXONOMY_READY`、36 categories、Provider OFF、0 provider calls、`SITE_SCAN_COMPLETED`。
- 点击“开始生产”且没有持久化 selected category 时，真实返回 HTTP 400：`开始生产前必须持久化至少一个已选类目/范围`；前端没有吞掉错误。
- `/delivery` 正确显示无交付批次、下载禁用；没有伪造 completed artifact。
- `/system` 显示 API/DB/L2 Chromium 状态、LOCAL_AGENT、Provider OFF_BY_DEFAULT；当前员工系统诊断中的 Blender 为 `NOT_CONFIGURED`，与本地 CLI 证据并列记录，未把 CLI 证据冒充已接入员工生产服务。

真实生产 D 复测：`E:\living\closure_evidence_20260905\production_d_interior_v2\d_result.json`，发现 9 candidates/9 unique、provider calls=0，进入 `BRAIN_DECISION` 后返回 `JOB_BLOCKED / LOCAL_AGENT_REVIEW_REQUIRED`，带 candidate/record IDs 和 `resume_safe=true`；因此没有 GLB/download PASS。

### Bridge 恢复

`E:\living\closure_evidence_20260905\bridge_recovery_v2\bridge_recovery_result.json`：`PASS`。同一 logical session 在重启后复用同一 request id；第二 turn 使用新 request；旧迟到响应被忽略；取消返回 `BrainCancelled / BRAIN_CANCELLED`；agent loop 的 stopped reason 同为 `BRAIN_CANCELLED`。

## R01–R15 状态

状态只使用附件允许的四种值。

| 项目 | 状态 | 实际证据/剩余工作 |
|---|---|---|
| R01 正面 -Y、顶部 +Z | FIXED | Blender orientation contract、det+1 rotation、12 个真实 CLI fixture 通过；倾斜语义阻断。 |
| R02 world bbox/父级/shared mesh | FIXED | glTF world transform、active roots、shared instance、多 mesh fixture 和 reimport JSON。 |
| R03 等比缩放 | FIXED | Blender CLI 一次 uniform scale、ground pivot、raw/final/reimport 三段尺寸。 |
| R04 未知单位 | FIXED | UNKNOWN_UNIT 与空/坏模型 edge evidence；不会默认米。 |
| R05 视觉拒绝与换图 | FIXED | 实际公开图片 bridge + 同 SKU gallery retry/rebind。 |
| R06 decode/load/媒体真实性 | FIXED | bytes、SHA256、JPEG/RGB/像素/alpha/variation 检查；坏图拒绝。 |
| R07 命名词表 | FIXED | formal vocabulary；未知 style/color/material/type 进入 REVIEW_REQUIRED。 |
| R08 唯一命名 | FIXED | 稳定 identity suffix，示例重复名唯一且 ≤50 字符。 |
| R09 尺寸与锚点 | FIXED | 轴级结构、partial/full/unknown、anchor policy 已在 pipeline/fixture 验证；供应商官方样本仍需 D。 |
| R10 官方尺寸查找 | BLOCKED_WITH_EVIDENCE | parser 和 evidence contract 已通过；45 站/公司正式 PDP 的完整官方尺寸尚未完成。 |
| R11 正式 API/runtime | BLOCKED_WITH_EVIDENCE | formal runtime、receipt、checkpoint、错误分类已接入；全矩阵尚有真实 defect/environment/partial。 |
| R12 C Agent recovery | BLOCKED_WITH_EVIDENCE | C 已走生产 acquisition factory 并携带 recovery callback；Durian v12 在真实公开发现超时，未到可证明 recovery PASS。 |
| R13 profile capability 复用 | FIXED | capability/status 持久化与 reuse；不把新 profile 当成隐形浏览器救援。 |
| R14 bridge 取消/重启/迟到/重复 | FIXED | recovery evidence 明确 PASS。 |
| R15 员工操作、Blender、下载 | BLOCKED_WITH_EVIDENCE | 员工真实 UI/API 和阻断显示通过；D/real GLB/download 尚未完成，不能发布。 |

## F01–F14 回归

完整 pytest 已通过，覆盖附件列出的空 profile/evidence、stale profile、official lookup、mixed units、provider quota、agent recovery、browser resume、robots、naming、image retry、job profile freeze、pagination exhaustion truthfulness、URL/log boundary 等回归路径；对应实现和测试位于 `services/api/tests`、`packages/workflow_core/tests`、`packages/workflow-engine/tests`。本轮没有把失败的真实外部站点改写成测试 PASS；外部不可达仍保留为 `ACCESS_BLOCKED`、`ENVIRONMENT_BLOCKED` 或 `TIMEOUT_REQUIRES_DIAGNOSIS`。

## G1–G10 本地验收门

| Gate | 状态 | 说明 |
|---|---|---|
| G1 最新 Final 基线/远端增量 | PASS | 未退回 `53331e2`；以 `1274554f` 为起点保留当前源码和远端 main 差异。 |
| G2 R01–R15 | BLOCKED | R10、R11、R12、R15 仍是 evidence-backed blocked。 |
| G3 Blender 真实归一化 | PASS | 真实 Blender 4.5.11：12/13 PASS，1 个倾斜模型正确阻断。 |
| G4 方向/多视图/重导入 | PASS | 方向、front/top/right/iso 和 reimport 有证据；倾斜语义需人工审。 |
| G5 图片/命名/尺寸 | PARTIAL | 本地真实媒体、词表、解析器通过；完整供应商官方尺寸未闭环。 |
| G6 三技术结构 A–D | BLOCKED | 还没有三类技术结构全部走完正式员工 D。 |
| G7 完整 45 站 | BLOCKED | 45 行完整记录存在，但 B/C 有 defect/environment/partial，D=45 NOT_RUN。 |
| G8 恢复/取消/重试 | PARTIAL | bridge recovery PASS，formal checkpoint/resume 存在；生产 D 尚未生成可交付 artifact。 |
| G9 员工 UI、GLB、下载 | BLOCKED | UI scan/error truthfulness PASS；GLB/delivery/download 未达门。 |
| G10 工程/公司接入 | PARTIAL | Python/前端/build PASS；公司 API/vision/runtime 未实连，UI Blender 仍 NOT_CONFIGURED。 |

## 公司正式接入步骤

公司提供真实兼容 OpenAI Responses/vision 的内部 endpoint、模型名和凭据后，在 API 进程环境配置（值不要写入仓库）：

```powershell
$env:WEBSITE_MODEL_MODE="TEXT_BRAIN_PLUS_VISION"
$env:WEBSITE_BRAIN_API_KEY="<company-secret>"
$env:WEBSITE_BRAIN_BASE_URL="https://<company-endpoint>/v1"
$env:WEBSITE_BRAIN_MODEL="<text-agent-model>"
$env:WEBSITE_VISION_API_KEY="<company-vision-secret>"
$env:WEBSITE_VISION_BASE_URL="https://<company-vision-endpoint>/v1"
$env:WEBSITE_VISION_MODEL="<vision-model>"
$env:WEBSITE_BRAIN_TIMEOUT_SECONDS="25"
$env:WEBSITE_BRAIN_CONNECT_TIMEOUT_SECONDS="5"
$env:WEBSITE_BRAIN_MAX_RETRIES="2"
$env:WEBSITE_BRAIN_RPM_LIMIT="30"
$env:WEBSITE_BRAIN_AGENT_ENABLED="true"
$env:PROVIDER_MODE="disabled"  # 只有已授权时才打开付费/公司 provider
```

接入顺序：

1. 启动 API/worker，打开 `/api/v1/control/system`，确认 company brain/vision capability、timeout、rate limit、provider ledger 可见。
2. 用同一 Interior Define job 选择至少一个持久化 category，运行同样的 B/C/D 入口；保存 request/turn/input hash、provider_posts、receipt、checkpoint。
3. 用一个完整官方尺寸 PDP 和一张视觉不合格图片验证：官方尺寸 evidence 不能被 AI 估算覆盖；失败图片必须在同一 SKU gallery 内 retry；不可达站点必须保持环境阻断。
4. 通过 45 站脚本重复 A/B/C，至少取三类技术结构完成 D；用同一批样本导出真实 glb，重新导入 Blender，检查 `-Y/+Z`、尺寸、front/top/right/iso 和 download hash。
5. 在 employee UI 选择 scope 后点击生产，确认 job recovery、cancel/late response、delivery batch、download URL 和 API/Blender/DB 状态一致，再由人工验收。

公司 API 未提供或凭据未授权时，本地 bridge 只能证明开发桥协议和恢复，不可宣称公司接入完成；这不改变本报告中已完成的本地证据。

## GitHub 和交付状态

- 本轮**没有执行 `git push`**，没有覆盖或改写 GitHub `main`，没有 force push，没有清空历史或业务数据。
- 本地提交包含上述源码和本报告；最终提交 SHA 将在交付消息中核验给出（不在报告内自引用 SHA，避免报告内容与自身 commit 形成循环）。
- 因 G2/G6/G7/G9/G10 未通过，不满足用户授权中“本地验收通过后再 push”的前置条件；CI/远端 SHA 因未 push 不会被虚报。
- 最终压缩包只包含可审查源码、配置、脚本和本报告，不包含 `.git`、`node_modules`、`.next`、缓存、运行时数据库、浏览器 profile 或外部证据目录。外部证据目录保留在 `E:\living\closure_evidence_20260905` 与 `E:\living\closure_evidence_20260906`，供网页端复核。
