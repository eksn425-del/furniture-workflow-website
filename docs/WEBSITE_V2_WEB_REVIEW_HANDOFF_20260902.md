# Furniture Workflow Website V2｜网页版联合核验反馈文档

日期：2026-09-02  
用途：将本文件直接交给网页版 AI，并要求它结合 GitHub 仓库源码进行独立核验  
仓库：<https://github.com/eksn425-del/furniture-workflow-website>  
核验分支：`main`  
最新提交：`6ed6e97f229c37a0b8d44aff09c44c77650e0a15`  
Website 代码修改提交：`1ef780f104ebf4cb67b317a9edcd203b73bab95e`

## 1. 给网页版 AI 的核验任务

请把本文件当作“测试背景和待核验问题”，不要把其中的结论直接当作事实。请先读取 GitHub `main` 的真实源码、`AGENTS.md`、README、handoff、测试报告和本文件，然后用源码、测试、Git diff 逐项验证：

1. 本次 V2 修改是否真正进入最新 `main`；
2. 报告中所说的修复是否贯穿 API → Runtime → Worker → UI，而不是只改了显示文字；
3. 类目、数量、Scope、Job 快照、分页、配额、访问状态、Local Agent、尺寸、Blender QA 和交付是否保持同一套数据契约；
4. 已知问题是否有 focused regression，而不是只凭一次人工点击声称解决；
5. 当前哪些能力已经通过确定性测试，哪些只在本机 Local Agent/Provider OFF 下验证，哪些必须在公司电脑真实配置后再验收；
6. 是否存在报告没有覆盖的新 P0/P1，特别是终态收敛、刷新恢复、多 Job 隔离、分页耗尽和真实模型交付问题。

请独立给出：

- `PASS`：源码和测试证据足够；
- `PARTIAL`：实现存在，但覆盖或真实环境证据不足；
- `FAIL`：源码与报告不一致，或测试可以稳定复现问题；
- `EXTERNAL_BLOCKED`：robots、CAPTCHA、登录、WAF、网络、真实 API 或本机软件条件造成的外部阻断。

不要因为某个第三方站点被阻断，就把整个 Website 判为失败；也不要因为 deterministic Fake E2E 通过，就把真实 Lux3D/Brain/Vision/Blender 判为已连接。

## 2. 版本和文件关系

### 2.1 GitHub 是代码唯一基线

本次最新 `main` 包含两个连续提交：

| 提交 | 内容 |
| --- | --- |
| `1ef780f` | V2 通用核心修复、回归测试、Holdout Register、成熟度总报告 |
| `6ed6e97` | 本联合核验反馈文档和上一份详细修改反馈文档 |

网页版应优先检查：

- [`AGENTS.md`](https://github.com/eksn425-del/furniture-workflow-website/blob/main/AGENTS.md)
- [`README.md`](https://github.com/eksn425-del/furniture-workflow-website/blob/main/README.md)
- [`docs/CROSS_SITE_AUTONOMOUS_MATURITY_REPORT_20260902.md`](https://github.com/eksn425-del/furniture-workflow-website/blob/main/docs/CROSS_SITE_AUTONOMOUS_MATURITY_REPORT_20260902.md)
- [`docs/WEBSITE_V2_CHANGE_FEEDBACK_20260902.md`](https://github.com/eksn425-del/furniture-workflow-website/blob/main/docs/WEBSITE_V2_CHANGE_FEEDBACK_20260902.md)
- [`docs/CROSS_SITE_HOLDOUT_REGISTER_20260902.md`](https://github.com/eksn425-del/furniture-workflow-website/blob/main/docs/CROSS_SITE_HOLDOUT_REGISTER_20260902.md)

### 2.2 旧 ZIP 的定位

用户提供的 `website_20260902_0.16.0.zip` 是本轮参考/现场反馈快照，不是最终代码基线：

- ZIP 文件时间早于 V2 执行文件；
- ZIP 内的 Website 版本仍为 `0.16.0`；
- ZIP 内有旧 handoff 和旧报告；
- ZIP 内没有本轮新增的 `CROSS_SITE_HOLDOUT_REGISTER_20260902.md` 和本次 V2 成熟度报告；
- 因此网页版不要用 ZIP 覆盖 GitHub，也不要以 ZIP 的旧测试结果否定已经进入 `main` 的修复。

正确关系是：

```text
ZIP = 现场快照 / 反馈来源
GitHub main = 脱敏后的正式源码基线
本文件 = 给网页版 AI 的核验上下文
```

## 3. 本次修改的核心反馈

### 3.1 类目与真实 Scope

旧风险：UI 粗类目可能合并多个原生子类目，但只保留一个代表 URL；UI 数量和实际采集范围不一致。

当前应核对：

- `services/api/app/services/native_site_analysis.py` 的 `_coarsen()` 是否保留全部成员 Scope；
- API 类目输出是否暴露 `scope_urls`；
- `services/api/app/services/product_acquisition.py` 是否展开粗类目成员 URL；
- Production Contract 是否仍保留稳定 `category_id`；
- 不同子类目数量不能只被加总到 UI，而实际只采集一个 URL。

预期结论：粗分类可以用于 UI，但选择粗分类后必须进入全部成员 Scope；数量不能脱离真实生产范围。

### 3.2 Job 快照不可漂移

旧风险：站点重新扫描后，旧 Job 可能读取新的可变 `SiteCategory`，导致 Scope、数量或 URL 变化。

当前应核对：

- `SiteCategorySnapshot` 是否是独立的不可变快照行；
- 复合身份是否为 `(snapshot_id, category_id)`；
- `site_scan_runtime.py` 是否先写快照，再更新当前读模型；
- `control_plane.py` 的 `_job_categories()` 是否优先从快照读取；
- 旧库 fallback 是否只使用 Job 原始 receipt，而不是回退到当前站点 taxonomy；
- `production_runtime.py` 是否向 Pipeline 传入 Job 创建时的 Scope。

应验证的序列：

```text
Scan A → 选择 A → 创建 Job A
→ Scan B → Resume Job A
```

Job A 必须继续使用 A 的 Scope。Scan B 可以改变 Site Library 当前视图，但不能改变已经锁定的 Job A。

### 3.3 稳定配额和策略

旧风险：用 `canonical_name` 作为配额 key；展示名称重名或重命名时发生串配额。页面存在 `PROPORTIONAL`/`CUSTOM`，但运行时没有真实语义。

当前应核对：

- `workers/production_pipeline.py` 是否使用 Scope/Category ID 分配配额；
- `PER_CATEGORY` 是否表示每个类目各满足目标；
- `TOTAL_ACROSS_SELECTED` 是否表示所有所选 Scope 合计满足目标；
- `EVEN` 是否真实均分；
- `PROPORTIONAL` 是否要求真实数量证据并按稳定规则分配；
- `CUSTOM` 是否要求每个 Scope 有配额且总和等于目标；
- `ASK`、`STOP`、`AUTO_IF_EXPLICIT` 是否实际影响 spillover；
- `apps/web/components/production-console.tsx` 的保存按钮是否真正提交策略，而不是只改本地状态。

### 3.4 Pagination 和 `visited != exhausted`

旧风险：访问过第一页就当作 Scope 耗尽，造成 false `TARGET_SHORTAGE`；Magento/PWA 反复读取 page 1。

当前应核对：

- checkpoint schema 是否升级为 v2；
- `visited_scopes` 与 `exhausted_scopes` 是否分开；
- `scope_cursors` 是否保存下一页、已看页面、页数、策略和终止原因；
- HTML 分页是否使用可验证 continuation；
- Magento GraphQL 是否传入 `currentPage` 并读取 `page_info/total_count`；
- 没有 continuation 但仍有产品时是否进入 `PAGINATION_UNVERIFIED`；
- `PAGINATION_UNVERIFIED` 是否阻止系统伪造 Exact-N shortage；
- 禁用的下一页或明确最终页是否可以标记 `EXPLICIT_END`。

这部分必须是 Generic Core 能力，不能只在 Interior Define 或某一个 Magento host 分支中成立。

### 3.5 访问状态与人工验证

当前应核对以下状态是否清晰分开：

| 外部证据 | 预期 Website 状态 |
| --- | --- |
| robots.txt 明确拒绝 | `ROBOTS_DENIED`，不升级 Browser 绕过 |
| 普通 403/430/405/429 或可重试服务端错误 | 根据证据进入 `BROWSER_REQUIRED` 或访问变更状态 |
| 明确可见 CAPTCHA/HUMAN 控件 | `HUMAN_REQUIRED`，保存 session/checkpoint |
| 临时导航或浏览器运行时失败 | 可恢复临时失败，不自动冒充 HUMAN |
| 官方尺寸区块被访问验证挡住 | `DIMENSIONS_BROWSER_HUMAN_REQUIRED` |

禁止网页版 AI 建议通过模拟点击、伪造 token、复制 Cookie、绕过 WAF 或破解 CAPTCHA 来“提高通过率”。

### 3.6 Local Agent、Vision 和媒体一致性

本机没有正式公司 Brain/Vision API。本轮使用 Website 的 `LOCAL_AGENT` 作为离线多模态 Reviewer。

当前应核对：

- Local Agent Review 是否能看到候选主图、来源页和身份/媒体证据；
- `reviewed_media_sha256` 是否与采集主图一致；
- hash 不一致是否阻止恢复；
- 非家具、场景图、Set/Bundle、第三方内容是否仍由 Gate 拒绝或进入人工判断；
- `TEXT_BRAIN_PLUS_VISION` 是否只是正式模式契约，不能在没有独立 Vision receipt 时宣称已经实连；
- Provider OFF 是否与 Review PASS、Modeling READY、Delivered 分开。

### 3.7 尺寸链和 Blender

尺寸优先级必须是：

```text
OFFICIAL_STRUCTURED
→ OFFICIAL_PAGE / L2
→ AI_ESTIMATED（官网确实无尺寸时）
→ target dimensions
```

当前应核对：

- 官方页面访问失败不会被当成官方缺失；
- `AI_ESTIMATED` 只有在官网无尺寸且 policy 允许时出现；
- GLB 是否先解析 raw bbox；
- 是否优先 uniform scale；
- Blender 是否执行 apply transform 后导出；
- 是否重新读取 final bbox 并做 tolerance QA；
- 明显 non-uniform deform 是否进入 `MODEL_DIMENSION_CONFLICT`；
- Blender 未配置时是否显示 `NOT_CONFIGURED`，不伪造交付。

## 4. 已有测试和真实证据

### 4.1 确定性测试

本次提交前已有以下结果：

| 检查 | 结果 |
| --- | --- |
| Python/API/workflow 全量 | 130 tests PASS |
| Website E2E + Blender targeted | 10 tests PASS |
| Python compileall | PASS |
| Web typecheck | PASS |
| Web lint | PASS，0 errors；1 个既有 `<img>` warning |
| Next.js production build | PASS |
| npm production audit | 0 vulnerabilities |
| public tree / sensitive scan | PASS |
| git diff --check | PASS |

确定性 E2E 的链路是：

```text
Local mock site
→ taxonomy EXACT=21
→ Exact-21 Job
→ discovery / identity / media / Local Agent / dimension / naming Gate
→ Fake Lux3D
→ Fake Blender QA
→ COMPLETED/SUCCEEDED
→ delivery ZIP 20 + 1
```

这证明状态机、快照、队列、收据、尺寸字段、Blender QA 和交付批次可以在受控环境闭环；不证明真实公司 API 已连接。

### 4.2 Interior Define canary

Interior Define 已降级为回归尺子：

- UI Bedroom Exact-3 Job 的源站数量显示为 186；
- Website 发现 `unique 9`，没有把未知数量错误显示为 0；
- Local Agent Review 能记录并恢复同一 Job；
- 通用采集器分别完成 Exact-3、Exact-25、Exact-50 探针；
- Snapshot A → Job A → Rescan B → Resume A 有回归覆盖；
- 真实公开商品页当前在官方尺寸获取阶段进入 `DIMENSIONS_BROWSER_HUMAN_REQUIRED`；
- Provider calls 为 0，未伪造真实 Lux3D 模型。

网页版不能把最后一条解释为“Website 爬虫坏了”，也不能把它解释为“真实建模已完成”；它是可恢复的外部访问/人工核验边界。

### 4.3 跨站结果

Development Set 的 11 站 S1 结果：`READY=2`、`PARTIAL=8`、`ROBOTS_DENIED=1`。

45-entry bounded S0（44 个历史站点 + West Elm）：

- `READY=30`
- `BROWSER_REQUIRED=4`
- `ROBOTS_DENIED=6`
- `FAILED=5`

Holdout 冻结为 8 个站点，没有新增专用 host adapter：

- Arhaus：`BROWSER_REQUIRED / HTTP_430`
- Nathan James：无专用规则完成 `Exact-1 READY`
- Kayu：外部 `Invalid IPv6 URL`
- Indian Hub：S0 READY
- Archive3D：网络策略失败
- GrabCAD：`ROBOTS_DENIED`
- NASA 3D：`ROBOTS_DENIED`
- Safavieh：`ROBOTS_DENIED`

这些结果表明 Generic Core 已能跨多个结构工作并进行真实分类，但 Holdout 还没有全部做到 S2/S3，跨站自治成熟度应为 `PARTIAL`，不能只根据 Interior Define 宣布全部泛化通过。

## 5. 需要重点查找的新问题

请网页版 AI 不要只复读已知问题，还要特别检查：

### 5.1 Job/Run/Event 终态收敛

- Job 已 `COMPLETED` 而 Run 仍 `RUNNING` 时，API 是否返回一致 read model；
- 重启 worker 后是否会重复写 Event；
- `runtime_event_id` 冲突是否幂等；
- refresh/polling 是否会把终态重新覆盖成旧状态；
- 同一 Job Resume 是否可能启动两个 worker/provider。

### 5.2 多页面和多任务隔离

- 两个 Job 同时 polling 时，是否串用 `scan_id`、candidate pool、event 或 selection；
- Site rescan 是否影响未选择该 Site 的 Job；
- 前端切换路由或刷新是否停止后台任务；
- `SiteCategory.selected` 是否被错误作为全局状态。

### 5.3 生产安全

- Provider OFF 是否阻止真实 POST；
- submission unknown 是否隔离而不是自动重提；
- cost approval、idempotency 和 provider ledger 是否有真实约束；
- Ready Pool 是否可能被 UI 误显示为已交付模型；
- 交付下载是否只允许 `DELIVERED Artifact`。

### 5.4 Exact-N 真实性

- reported aggregate count 是否被错误当成 unique supply；
- Variant 是否以稳定 product identity 去重；
- 访问不到的商品是否被写成 0；
- Target shortage 是否有真实 exhausted/pagination 证据；
- spillover 是否能覆盖一个类目缺货，但不突破用户选择的 STOP/ASK 策略。

## 6. 站点专用规则过拟合核验

请统计本次 V2 之后：

- 新增 host-specific `if/else` 数量；
- 新增 Site Adapter 数量；
- 新增 Generic strategy 数量；
- 新增通用 regression/browser E2E 数量。

当前预期：

- Holdout 专用规则：`0`；
- Holdout 专用 fixture：`0`；
- 主要修改集中在通用 Scope、snapshot、cursor、access state、quota、dimension 和 GLB QA。

如果网页版发现某个新站点必须再添加一个 host 分支才能通过，请判断它是否真的属于独特技术结构；不能为了把结果变绿而过拟合。

## 7. 安全与隐私核验

仓库只应保留：

- 源代码；
- 空的 `.env.example`；
- 不含真实值的配置说明；
- 测试 placeholder；
- 脱敏后的报告和文档。

不得出现：

- API keys、Bearer token、密码；
- 公司内网地址、私有 endpoint；
- 个人浏览器 Cookie/Profile；
- 本机绝对路径；
- 数据库、日志、截图、运行产物、GLB/ZIP 模型。

## 8. 网页版最终输出格式

请最终按下表给出独立判断：

| 审查项 | PASS/PARTIAL/FAIL/EXTERNAL_BLOCKED | 证据文件/测试 | 说明 |
| --- | --- | --- | --- |
| GitHub 版本一致性 |  |  |  |
| Site Library / Site Detail |  |  |  |
| New Job / Job Detail |  |  |  |
| 类目数量与 Scope |  |  |  |
| Snapshot 不漂移 |  |  |  |
| Pagination / ScopeCursor |  |  |  |
| Quota / Spillover |  |  |  |
| Access / HUMAN 状态 |  |  |  |
| Local Agent / Vision receipt |  |  |  |
| 官方尺寸链 |  |  |  |
| Blender bbox / QA |  |  |  |
| Provider safety |  |  |  |
| Delivery |  |  |  |
| 跨站 Generic Core |  |  |  |
| Holdout zero-shot |  |  |  |
| 脱敏与公开树 |  |  |  |

最后请明确回答：

1. 这次 `main` 是否真的包含 V2 修改？
2. 哪些问题已经有源码和回归证据证明修复？
3. 哪些问题只是安全阻断或公司环境未激活，不能算代码失败？
4. 是否还存在 P0/P1？
5. 是否同意把当前版本用于公司电脑的受控 Brain + Lux3D + Blender 试点？
6. 是否同意把“所有任意网站都能无人工完成建模”作为尚未证明的目标，而不是当前已完成事实？

## 9. 一句话交接结论

请结合本文件和 GitHub `main` 做独立代码审查：当前 Website 已从“单站能跑”推进到“通用控制面、证据链、恢复和安全 Gate 基本成形”，适合公司环境受控试点；但真实外部服务激活、独立 Vision receipt、Blender 现场配置以及全部 Holdout 的零样本深度生产仍必须单独验收，不能被本机 Fake E2E 或单一站点结果替代。
