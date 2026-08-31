# Website 端到端修复与自审报告

日期：2026-08-31（本轮更新）
范围：`FurnitureWorkflow-Website-public`（仅 Website 源码）

## 1. 结论

本轮目标是把公开 Website 副本从“可以展示控制台和启动任务”推进到一条可重复验收的完整链路：

`Add Site → Preflight → Taxonomy/Count → Scope Selection → Job → Discovery → Identity/Media/Vision → Dimension → Naming → Production Gate → Lux3D boundary → Blender normalize/QA → 20-per-batch delivery`

新增的 `LOCAL_E2E` 只在测试显式设置 `FURNITURE_WORKFLOW_LOCAL_E2E=1` 且 Job policy 明确 `test_profile=LOCAL_E2E` 时启用。它使用本地确定性 Commerce、Local Agent、Fake Lux3D 和 Fake Blender，不访问外网、不发送真实 Provider 请求，也不能被普通生产任务隐式启用。

本仓库不包含 Skills 包，不读取、复制或启动 Skills；不包含 API key、密码、Cookie、浏览器 Profile、公司资料、运行数据库、抓取媒体或 GLB 产物。

## 2. Baseline 与主要根因

Baseline 已先执行原有后端测试集与前端 TypeScript/lint/build，作为本轮修改前基线。代码检查确认 Website 的主要缺口不是重新设计 UI，而是以下端到端断点：

1. 扫描仍在运行、被人工验证阻断或失败时，读模型可能把旧类别或空结果当成当前 `0`。
2. Job 选择的类别没有稳定绑定到选择时的 taxonomy snapshot，后续扫描可能改变实际运行范围。
3. 父类/子类计数汇总存在重复计算风险；未知子类不能被静默当作已知数量相加。
4. Provider 下载后只有原始 GLB 容器检查，没有强制经过 Blender 归一化和 QA；交付没有按 20 个一批形成独立可下载收据。
5. Blender QA 的路径、哈希和状态没有完整回写 Candidate Pool lineage，控制台无法核对同一产品是否真正完成后处理。
6. 没有一个不依赖真实站点、真实 Brain、真实 Lux3D 的 API 级完整回归，无法证明“新建网站到下载交付”真的连通。

## 3. 实施内容

### 3.1 可信类目与数量语义

- 增加统一 taxonomy read model：当前扫描 `QUEUED/ANALYZING/L2_BROWSER` 返回 `SCANNING + UNKNOWN`，阻断返回 `HUMAN_REQUIRED/BROWSER_REQUIRED/... + UNKNOWN`，不会把历史快照伪装成当前成功结果。
- Job 选择只接受当前可用 taxonomy，并保存 `category_snapshot_id`；运行时按 snapshot 与选定 category IDs 构造合同。
- 站点总计优先使用一级类目，避免父子行重复相加。
- 合并二级类目时，只有全部成员有可靠数量才允许形成 `EXACT/ESTIMATED`；任何未知成员都会保留 `UNKNOWN`。
- 页面使用“未知/当前数量不代表 0”等明确状态，扫描失败时保留历史证据与恢复入口。

### 3.2 产品、图片、视觉与命名

- Candidate Pool 保留 acquisition/source/image/vision/gate 的 lineage，并将 post-processing 证据继续写回。
- 本地 E2E 产品具有稳定的 source product ID、canonical URL、配置键、主图角色、身份匹配、scope 与 Local Agent review 证据。
- 正式运行仍沿用既有三种决策模式边界：`LOCAL_AGENT`、`MULTIMODAL_SINGLE_MODEL`、`TEXT_BRAIN_PLUS_VISION`；本次没有把本地模拟器伪装成公司 API。
- 最终名称继续走现有命名 Gate；测试断言每个最终名称（包含空格）不超过 50 个字符。名称压缩只服务于交付文件唯一性，不能篡改来源身份。

### 3.3 Provider、Blender 与交付

- Provider qualification 同时要求 `LUX3D_API_KEY` 和 `LUX3D_BASE_URL` 非空；空配置不会被判定为可付费生产。
- 公开站点的 L2 采集默认依赖隔离的 Playwright Chromium；本地执行需先运行 `python -m playwright install chromium`，或显式选择已安装的 `msedge`/`chrome` 通道。每个 Job 继续使用独立持久 profile，不读取个人浏览器 profile。
- Provider ledger 继续承担幂等键、已知 task 恢复和未知提交隔离；本地 E2E 的 Provider 仅为显式测试组件。
- 新增显式 Blender adapter boundary：
  - `LOCAL_E2E` 使用 `FakeBlenderAdapter`，只做确定性复制与 GLB 容器 QA；
  - 生产环境只有在 `BLENDER_WORKER_ENABLED=true` 且 `BLENDER_EXECUTABLE` 可解析时才使用 headless Blender CLI；
  - 没有适配器或归一化/QA 失败时硬阻断交付，不把 raw GLB 当成已交付模型。
- 下载阶段校验 GLB magic/version/declared length，经过 Blender 后再写入 `normalized_glb_path`、哈希、大小、QA 状态。
- 交付阶段按 20 个模型生成 `batch_001.zip`、`batch_002.zip` 等，每个 ZIP 内含 `manifest.json` 和对应 GLB；根 `manifest.json` 汇总批次、哈希、路径、模型和 QA 收据。
- API 优先展示 `DELIVERY_BATCH_ZIP`，并提供可下载的 ZIP，而不是只显示运行目录。

### 3.4 本地可重复 E2E

`services/api/tests/test_website_e2e.py` 从 API 层执行完整链路：

1. 加入 `https://local.mock/` 并完成预检；
2. 扫描并保存一个有 `EXACT=21` 的 `Chairs` taxonomy snapshot；
3. 创建 `EXACT_N=21` Job，并将当前 snapshot 的类别绑定到 Job；
4. 注入显式 `LOCAL_E2E` 测试 profile 与 `provider_concurrency=5`；
5. 批量发现、身份/图片/视觉/尺寸/命名审核；
6. 21 次确定性 Fake Lux3D task 创建、轮询与 GLB 下载；
7. 21 次 Fake Blender normalize/QA；
8. 验证 `COMPLETED/SUCCEEDED`、21 个 completed candidates、21 个 provider tasks、全部命名 ≤50、全部 Blender QA `PASS`；
9. 验证 20+1 两个 ZIP 批次、manifest、API 下载和 ZIP 内 GLB 数量。

## 4. 配置边界

`.env.example` 只列变量名和安全默认值，不列任何真实值。正式公司环境由本地私有 `.env.local` 或服务管理器注入：

- Brain：`WEBSITE_BRAIN_API_KEY`、`WEBSITE_BRAIN_BASE_URL`、`WEBSITE_BRAIN_MODEL` 及重试/限流变量；
- Lux3D：`LUX3D_API_KEY`、`LUX3D_BASE_URL`、版本、面数和轮询变量；
- Blender：`BLENDER_WORKER_ENABLED`、`BLENDER_EXECUTABLE`；
- 成本与授权：`AUTO_MODEL_COST_CEILING_MINOR`、`MODEL_ESTIMATED_COST_PER_ITEM_MINOR`、`EXACT_COUNT_AUTHORIZATION`。

默认 Provider 关闭。真实站点/真实 Brain/真实 Lux3D 未在本地回归中伪造为成功；没有配置时只能到 Ready Pool 或明确阻断。真实采集仍遵守 robots、条款、限流和人工验证，不实现 CAPTCHA/WAF/登录绕过。

## 5. 计划执行的最终验证

以下命令是提交前的固定验收清单，最终结果以本报告随提交版本的命令输出为准：

```powershell
$repoRoot = (Get-Location).Path
$env:PYTHONPATH = "$repoRoot;$repoRoot\services\api;$repoRoot\packages\workflow-engine\src"
python -m pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests
python -m pytest -q services/api/tests/test_website_e2e.py
```

```powershell
Set-Location apps/web
npm run typecheck
npm run lint
npm run build
Set-Location ..\..
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_public_tree.ps1
git diff --check
```

本轮最终执行结果（2026-08-31 工作树）：

- Python 全量回归：`102 passed`；其中本地完整链路 E2E：`1 passed`。
- Python `compileall`：PASS。
- Web `npm run typecheck`：PASS。
- Web `npm run lint`：PASS；仅保留 1 个既有的 `@next/next/no-img-element` 非阻断警告，无错误。
- Web `npm run build`：PASS，Next.js 生产页面生成完成。
- `PUBLIC_TREE_CHECK`：PASS（124 个文件）；内容级凭据扫描：PASS；本机绝对路径扫描：PASS；`git diff --check`：PASS。

## 6. 自审与已知边界

- 只改 Website 仓库，Skills 不在本次变更范围内。
- 不把测试 fixture、假 Provider 结果或空配置当作真实生产成功。
- 不为凑 Exact-N 放宽身份、图片、视觉、尺寸、命名或交付 Gate。
- Provider 槽位上限仍受配置和安全边界约束；本地 E2E 只验证显式的 5 槽合同能够进入运行时，不代表真实账号一定允许 5 个并行请求。
- headless Blender CLI 是适配接口，不在没有安装 Blender 的机器上声称已经完成真实 Blender 运行；缺失时会显示 `BLENDER_NOT_CONFIGURED` 并阻断交付。
- 真正公司环境仍需单独做小规模 live smoke、人工验证接管、真实 Brain/Vision 连通和真实 Lux3D 付费确认；这些结果必须与离线 E2E 分开记录。

## 7. 2026-08-31 真实 UI 回归与本轮修复

本轮使用可见的 Website 浏览器页面完成连续 UI 操作，验证了新站点导入、摸底、类目数量、类目选择、建任务、L2 发现、Local Agent 复核、恢复同一 Job 和安全阻断。未调用真实 Brain/Vision 或 Lux3D Provider，也没有为了通过测试放宽生产 Gate。

### 7.1 Interior Define：失败证据与有界重试

- Dining → All dining tables 页面显示当前数量 `22`，选择汇总 `22`；Exact-2 Job `job_b9495c7c8aae4284afd8c70277741054`。
- 首次运行 `run_f4bda66d8c204ec4850d78c5a9cfeaa3` 真实复现了 Worker 启动后无终态事件的问题；保留了失败状态、工作区合同和日志证据，没有把失败伪装成成功。
- 通过新增的空事件有界重试，恢复同一 Job 后运行 `run_06e15bedff1c4ec2b3817350a1d81369` 成功输出 `JOB_STARTED`、`BRAIN_BOUNDARY_CHECKED`、`DISCOVERY_COMPLETED`（6 个唯一候选），随后按 Local Agent 规则安全暂停复核；没有重复候选或 Provider 调用。
- 启动信息写入 `website_runtime_launcher.log`，Python 以无缓冲模式启动，方便定位“进程已启动但事件未落盘”的问题。

### 7.2 Francfranc：跨类目与数量语义

- `Living Storage` 页面显示约 `94` 件，创建 Exact-1 Job `job_d814e04e071240a29e21714fcc0b2d75`。
- L2 发现 3 个唯一候选；第一候选完成 Local Agent 复核并恢复同一 Job，因缺少深度字段被 `DIMENSION_REJECTED`，没有为了凑数量继续放行。
- 同一站点其他类目中已观察到 `EXACT` 数量和 `UNKNOWN` 数量并存，页面不会把未知类目当作 0 或假精确总数。

### 7.3 MUJI：新站点导入与跨站点类别 ID 冲突

- 首次 MUJI 摸底真实失败，错误证据为旧分析器复用路径型 `category_id`，SQLite 报 `UNIQUE constraint failed: site_categories.category_id`；失败快照和错误状态被保留。
- 最小修复：持久化层检测跨站点/跨路径的旧 ID 冲突，并生成确定性的站点级类别 ID；同站点既有路径 ID 保持稳定，避免破坏已有任务引用。
- 修复后重新摸底完成 `47` 个类目（`PARTIAL`），`Accessories` 当前数量显示 `56`；选择汇总 `56`，创建 Exact-1 Job `job_12e35981d9844b5180010f039ebc3d16`。
- L2 发现 3 个候选；`Hakama Pants` 经 Local Agent 判定为服装/非家具，保存为 `VISUAL_REJECTED`，剩余候选保留在同一候选池等待处理，没有把非家具当作建模产品，也没有 Provider 调用。

### 7.4 本轮部署边界

- 本地服务运行在 `LOCAL_AGENT`/`WEBSITE_MODEL_MODE=LOCAL_AGENT`，Provider 保持 OFF；这些 UI 结果证明的是 Website 控制面、状态机、证据链和安全闸门，不是付费模型服务已连通。
- 未来公司环境仍可分别注入 Brain（或单一多模态模型/文本 Brain + Vision）和 Lux3D 配置；配置后还必须进行独立的 live smoke，确认真实认证、限流、费用闸门、人工验证接管以及模型下载/Blender QA。
- 真实站点允许出现 `PARTIAL`、`UNKNOWN`、`HUMAN_REQUIRED` 和产品级拒绝；这属于系统对证据不足的诚实表达，不应在部署时改成默认放行。

## 8. 发布记录

- GitHub repository：`https://github.com/eksn425-del/furniture-workflow-website`
- 功能修复提交：`002950d`（`Complete Website end-to-end workflow repair`），已直接推送到 `main`。
- 本报告的最终测试统计随随后续文档校正提交推送；最终远端 `main` SHA 在交付消息中完成对账。
- 本轮（2026-08-31）仅完成当前工作树的修复、回归和报告更新，尚未创建新的 commit 或 push；提交前应重新审查工作树并执行安全检查。
- 本报告不保存任何 API key、Cookie、个人信息、公司内部地址或运行产物。
