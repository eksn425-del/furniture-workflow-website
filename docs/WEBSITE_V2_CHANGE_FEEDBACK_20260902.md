# Website V2 修改反馈与网页版核验报告

日期：2026-09-02  
项目：Furniture Workflow Website  
GitHub：`eksn425-del/furniture-workflow-website`  
核验分支：`main`  
当前提交：`1ef780f104ebf4cb67b317a9edcd203b73bab95e`

## 1. 先给结论

本次依据《Furniture Workflow Website｜最终跨网站自治成熟度测试 V2》及其启动 Prompt 完成的 Website 修改，已经推送到 GitHub `main`。

本次不是只改 UI，而是针对 Website 在真实使用中暴露出的数据契约、采集分页、任务恢复、访问分类、尺寸来源、配额和 Blender QA 问题做了通用修复。

当前判断：

| 范围 | 状态 |
| --- | --- |
| Website 控制面和本地工作流代码 | `COMPANY LARGE-SCALE PILOT READY` |
| 前端构建、后端测试、确定性 E2E | `PASS` |
| 跨站零样本成熟度 | `PARTIAL`，已覆盖多种结构，但 Holdout 尚未全部完成深度生产 |
| 本机真实 Brain/Vision/Lux3D | `OFF / ACTIVATION GATED` |
| 本机真实 Blender 生产链 | 代码已补齐，是否可执行取决于部署机 Blender 配置 |

本报告用于网页版 AI 对照 GitHub 源码核验，不包含任何 API key、私有 URL、个人信息、浏览器 Cookie/Profile、数据库、运行日志或模型产物。

## 2. GitHub 版本确认

### 2.1 当前远端状态

- `origin/main` 与本地 `HEAD` 均为 `1ef780f104ebf4cb67b317a9edcd203b73bab95e`。
- 提交信息：`Harden cross-site autonomous workflow maturity`。
- Push 已成功，未使用 force push。
- 工作树在提交后保持干净。

网页版核验入口：

- 仓库主页：<https://github.com/eksn425-del/furniture-workflow-website>
- 当前提交：<https://github.com/eksn425-del/furniture-workflow-website/commit/1ef780f104ebf4cb67b317a9edcd203b73bab95e>
- 本报告：<https://github.com/eksn425-del/furniture-workflow-website/blob/main/docs/WEBSITE_V2_CHANGE_FEEDBACK_20260902.md>
- V2 成熟度总报告：<https://github.com/eksn425-del/furniture-workflow-website/blob/main/docs/CROSS_SITE_AUTONOMOUS_MATURITY_REPORT_20260902.md>
- Holdout 冻结名单：<https://github.com/eksn425-del/furniture-workflow-website/blob/main/docs/CROSS_SITE_HOLDOUT_REGISTER_20260902.md>

### 2.2 本次提交包含的主要文件

| 区域 | 主要文件 |
| --- | --- |
| 前端任务策略和配额 | `apps/web/components/production-console.tsx`、`apps/web/lib/console-api.ts` |
| 类目快照和任务 API | `services/api/app/models.py`、`services/api/app/schemas.py`、`services/api/app/api/routes/control_plane.py` |
| SiteScan/类目分析 | `services/api/app/services/site_scan_runtime.py`、`services/api/app/services/native_site_analysis.py` |
| 商品采集与分页 | `services/api/app/services/product_acquisition.py` |
| Production 合同 | `services/api/app/services/production_runtime.py`、`packages/workflow-engine/src/furniture_workflow_engine/runtime.py` |
| Pipeline/尺寸/访问阻断 | `workers/production_pipeline.py` |
| 回归测试 | `services/api/tests/test_native_site_analysis.py`、`test_product_acquisition.py`、`test_production_convergence.py`、`packages/workflow-engine/tests/test_production_runtime.py` |
| V2 文档 | `docs/CROSS_SITE_HOLDOUT_REGISTER_20260902.md`、`docs/CROSS_SITE_AUTONOMOUS_MATURITY_REPORT_20260902.md` |

## 3. 针对已知问题的修改反馈

### 3.1 UI 粗类目与真实 Production Scope 不一致

问题：多个原生子类目被 UI 合并为一个粗类目时，旧逻辑可能只保留一个代表 URL，却把多个子类目的数量相加。结果可能是 UI 显示数量足够，但采集实际只访问了一个子类目。

修改：

- `NativeSiteAnalyzer._coarsen()` 保存粗类目全部成员的 `scope_urls` 和证据；
- API 的类目输出暴露 `scope_urls`；
- `ProductAcquisitionEngine._compact_scopes()` 会展开粗类目的全部成员 URL；
- 生产合同保留稳定 `category_id`，不再只依赖展示名称和代表路径。

网页版应核对：

- 勾选粗类目后，Job Contract 中是否保留多个真实 Scope；
- UI 数量和采集 Scope 是否来自同一组范围；
- 不应出现“显示 30 个、实际只进入一个子类目”的静默错位。

### 3.2 旧 Job 随站点重新扫描发生 Scope 漂移

问题：旧的 `SiteCategory` 行是可变读模型。站点重新扫描后，如果复用同一个 category ID，旧 Job 可能查不到原快照，或错误读取最新类目。

修改：

- 新增 `SiteCategorySnapshot` 不可变快照表；
- 使用 `(snapshot_id, category_id)` 复合身份；
- SiteScan 完成时先保存快照，再刷新当前可变类目；
- Job Contract 优先从绑定快照读取；
- 对旧数据库保留 receipt fallback，但不会静默切换到当前 Site taxonomy。

网页版应核对：

```text
Scan A → 选择 Category A → 创建 Job A
→ 重新 Scan B → Resume Job A
```

Job A 应继续使用 A 的 Scope、数量证据和 URL；不能被 B 覆盖。

### 3.3 Quota 使用 canonical name，重名时不稳定

问题：展示名称可能重名、改名或因 Brain 规范化而变化，用它作为配额 key 会造成多个类目串配额。

修改：

- `category_id` 成为配额和 lineage 的首选稳定 key；
- `PER_CATEGORY`、`EVEN`、`PROPORTIONAL`、`CUSTOM` 均按 scope ID 计算；
- 旧 fixture 没有 scope ID 时才使用兼容 fallback；
- 前端 Custom Quota 的保存和校验使用真实 scope ID。

网页版应核对：

- 两个显示名称相同的类目仍能分别配置配额；
- rename 后配额不会迁移到错误类目；
- Custom 总和不等于目标数时不能保存或启动。

### 3.4 Pagination / Scope Cursor 不真实

问题：旧逻辑把“访问过第一页”近似成“该 Scope 已耗尽”，导致 false `TARGET_SHORTAGE`；Magento/PWA 还可能永远从 `currentPage=1` 读取。

修改：

- acquisition checkpoint 升级为 v2；
- 分开保存 `visited_scopes`、`exhausted_scopes`、`scope_cursors`；
- 每个 cursor 保存 `next_url`、已看页面、页数、策略、终止原因；
- 识别 `rel=next`、下一页链接、query/page cursor、禁用下一页和 Magento GraphQL `page_info`；
- 没有可验证 continuation 时标记 `PAGINATION_UNVERIFIED`，不标记 exhausted；
- 目标不足但分页未证实时不会伪造 shortage 结论。

网页版应核对：

- `visited != exhausted` 能在状态和事件中区分；
- 第 2 页、第 3 页产品不会与第一页重复；
- Exact-N 不能用单页可见数量直接判定全站短缺；
- Magento `current_page` 会递增，`total_count/total_pages` 会进入 checkpoint。

### 3.5 ALL、PROPORTIONAL、CUSTOM 和 Spillover 只是 UI

问题：策略字段在页面上存在，但如果没有真正进入运行时合同，就会形成“看起来可选、实际仍按默认行为”的假功能。

修改：

- Job 创建和编辑 API 接收并持久化 `category_allocation`、`allocation_strategy`、`spillover`、`category_quotas`；
- `PROPORTIONAL` 要求真实数量证据，按最大余数法分配；
- `CUSTOM` 要求每个 scope 都有配额且总和严格等于目标；
- `ALL + CUSTOM` 不能通过 UI 保存；
- `ASK`、`STOP`、`AUTO_IF_EXPLICIT` 在 engine 中有不同执行语义；
- 旧直接调用者使用兼容默认值，但 Website 生产合同传入显式策略。

网页版应核对：

- `ALL` 代表所选范围的完整公开供应，不是把未知数量当 0；
- `ASK` 出现缺口时暂停等待决策；
- `STOP` 不跨类目吞掉已锁定份额；
- `AUTO_IF_EXPLICIT` 只有用户明确选择时才允许 spillover；
- Custom 配额修改后，刷新页面仍保留。

### 3.6 Robots、WAF、临时失败和 HUMAN_REQUIRED 混淆

问题：robots 拒绝、普通 403、临时浏览器失败和明确 CAPTCHA/HUMAN 控件不能使用同一个状态，否则会错误绕过规则或要求用户处理不存在的验证。

修改：

- `RobotsDenied` 不升级 Browser；
- 401/403/405/429/430、可重试服务端错误按证据决定是否进入 L2；
- 明确可见的人机控件才进入 `HUMAN_REQUIRED`；
- Browser 临时错误进入可恢复的临时失败，不直接冒充 HUMAN；
- L2 无法访问官方尺寸时进入 `DIMENSIONS_BROWSER_HUMAN_REQUIRED`，不降级伪造 AI 尺寸。

网页版应核对：

| 外部信号 | Website 预期 |
| --- | --- |
| robots.txt 拒绝 | `ROBOTS_DENIED`，不自动换 Browser |
| 普通 HTTP 403/430 | `BROWSER_REQUIRED` 或 `ACCESS_CHANGE_REQUIRED`，依证据决定 |
| 明确 CAPTCHA/HUMAN 控件 | `HUMAN_REQUIRED`，保存 checkpoint |
| 临时导航失败 | `TEMPORARY_FAILURE`，可恢复 |
| 官方尺寸页面被验证挡住 | `DIMENSIONS_BROWSER_HUMAN_REQUIRED` |

### 3.7 Vision 没看图却标记 PASS

问题：本机没有正式 Brain/Vision API 时，不能把“没有调用远程服务”误报为完成视觉审核。

修改：

- 本机默认 `LOCAL_AGENT`，由 Website UI/本地 Agent 写入结构化 receipt；
- receipt 必须绑定候选主图的 `reviewed_media_sha256`；
- 图片证据不一致时不允许恢复生产；
- 正式 `MULTIMODAL_SINGLE_MODEL` 与 `TEXT_BRAIN_PLUS_VISION` 配置契约保留；
- 本轮没有假装完成独立公司 Vision Provider 验证。

网页版应核对：

- Local Agent Review 页面显示主图、来源和媒体 hash；
- 提交复核时 hash 不匹配会拒绝；
- `TEXT_BRAIN_PLUS_VISION` 没有独立 receipt 时不应显示为已完成；
- Provider OFF 不等于视觉审核通过，也不等于建模成功。

### 3.8 官方尺寸访问失败被当作缺失

问题：官网商品页的尺寸区块可能因为 JS、L2 或访问验证暂时不可读。直接落到 AI 估算会把访问失败伪装成官网无尺寸。

修改：

- 尺寸链固定为：

```text
OFFICIAL_STRUCTURED
→ OFFICIAL_PAGE / L2
→ AI_ESTIMATED（仅官方确实缺失）
→ target dimensions
```

- `BrowserHumanRequired` 返回可恢复的 PENDING；
- `BrowserRuntimeMissing` 不被吞掉；
- manifest、candidate lineage 和 model input 保存 dimension source、值和单位。

网页版应核对：

- 页面被挑战时不能出现 `AI_ESTIMATED`；
- 官网有结构化字段时不能被 Brain 估算覆盖；
- 真正缺少尺寸时才允许 `AI_ESTIMATED`，并明确标记。

### 3.9 Blender 尺寸链不完整

问题：仅导入/导出 GLB 不能证明模型已按目标尺寸校准。

修改：

- 解析 GLB POSITION bbox；
- 计算 raw bbox 与 target dimensions 的比例；
- 优先 uniform scale；
- 调用 Blender import、scale、apply transform、export；
- 再读 final bbox；
- 按 tolerance 做 QA；
- 明显非均匀变形抛出 `MODEL_DIMENSION_CONFLICT`。

网页版应核对：

- raw bbox、target dimensions、final bbox 都进入 manifest；
- uniform scale 时三轴比例一致；
- 冲突尺寸不会被硬拉伸后标记 PASS；
- 没有配置 Blender 时显示 `NOT_CONFIGURED`，不显示为已交付。

## 4. 本轮真实测试证据

### 4.1 Interior Define canary

Interior Define 只作为回归尺子：

- Website UI 已创建 Bedroom Exact-3 Job；
- 页面显示源站数量 186；
- 发现 `unique 9`，没有误显示成 0；
- Local Agent 复核可记录并恢复同一 Job；
- 通用采集器独立探针取得 Exact-3、Exact-25、Exact-50；
- Snapshot A → Job A → Rescan B → Resume A 已有回归覆盖；
- 当前真实商品尺寸页无法稳定取得官方证据时，任务安全暂停为 `DIMENSIONS_BROWSER_HUMAN_REQUIRED`；
- Provider calls 为 0，没有把停机伪装成真实 Lux3D 建模成功。

### 4.2 Development Set

本轮对 11 个 Development Site 做了 bounded S1：

| 站点 | 结果 | 类目数 | 备注 |
| --- | --- | ---: | --- |
| Interior Define | READY | 36 | canary |
| Article | PARTIAL | 8 | 混合类目/商品结构 |
| Castlery | PARTIAL | 27 | 动态零售 |
| Anthropologie | ROBOTS_DENIED | — | 不绕过 robots |
| Room & Board | PARTIAL | 67 | 高摩擦压力测试 |
| West Elm | PARTIAL | 13 | 访问/数量证据不完整 |
| CGTrader | PARTIAL | 26 | Marketplace，保留既有筛选策略 |
| DesignConnected | PARTIAL | 22 | 设计目录 |
| Poly Haven | PARTIAL | 14 | 资源目录，不误当家具商品 |
| Alessi | READY | 15 | 零售目录 |
| Fabuliv | PARTIAL | 28 | 区域零售 |

统计：`READY=2`、`PARTIAL=8`、`ROBOTS_DENIED=1`。

### 4.3 Holdout

冻结的 8 个 Holdout 没有新增站点专用规则：

| 站点 | S0 | 第一次深探针 |
| --- | --- | --- |
| Arhaus | `BROWSER_REQUIRED / HTTP_430` | 保留人工/可见会话入口 |
| Nathan James | `READY` | 无专用规则完成 `Exact-1 READY` |
| Kayu | `READY` | 外部 `Invalid IPv6 URL`，保留为网络阻断 |
| Indian Hub | `READY` | 可进入后续 S2 |
| Archive3D | `FAILED / NETWORK_POLICY` | 保留证据 |
| GrabCAD | `ROBOTS_DENIED` | 不绕过 |
| NASA 3D | `ROBOTS_DENIED` | 不绕过 |
| Safavieh | `ROBOTS_DENIED` | 不绕过 |

这证明 Generic Core 已能在至少一个未专门适配的 Holdout 上完成 S1 + Exact-1，但还不足以宣布所有 Holdout 全部成熟。

### 4.4 45 站 bounded S0

44 个历史站点加 West Elm 的 S0 分类：

| 状态 | 数量 |
| --- | ---: |
| `READY` | 30 |
| `BROWSER_REQUIRED` | 4 |
| `ROBOTS_DENIED` | 6 |
| `FAILED` | 5 |
| 合计 | 45 |

S0 只证明入口状态分类，不代表该站已完成商品发现、视觉审核、Lux3D 建模或交付。

## 5. 测试门禁

本次提交前完成：

| 检查 | 结果 |
| --- | --- |
| Python/API/workflow 全量 | `130 tests PASS` |
| Website E2E + Blender targeted | `10 tests PASS` |
| Python compileall | PASS |
| Web typecheck | PASS |
| Web lint | PASS，0 errors；1 个既有 `<img>` warning |
| Next.js production build | PASS |
| npm audit（production） | `0 vulnerabilities` |
| public tree / sensitive scan | PASS，未发现真实 key 或运行时文件 |
| git diff --check | PASS，仅有 Windows 换行提示 |

确定性 E2E 验证：

```text
Exact-21
→ 21 个候选完成
→ Fake Lux3D ledger
→ Fake Blender QA PASS
→ 20 + 1 交付批次
→ ZIP + manifest
```

该 Fake 流程只在显式 Local E2E profile 下启用，不会污染普通生产任务。

## 6. 尚未解决/需要网页版重点核验的事项

这些不是被隐藏的问题，而是当前明确的剩余边界：

1. 本机没有启用真实 Brain/Vision/Lux3D，所以不能用本机结果证明公司 API 已接通。
2. `TEXT_BRAIN_PLUS_VISION` 的契约保留，但本轮没有独立 Vision Provider 实连验收。
3. Interior Define 当前真实尺寸页面需要 L2/人工核验，系统正确暂停；这不是数量扫描失败。
4. Holdout 中只有 Nathan James 完成了第一次 Exact-1 深探针，其他站点因 robots、网络或 Browser 条件没有全部进入 S2。
5. 真实 Blender 是否可执行，要在公司电脑的 `BLENDER_EXECUTABLE` 和 worker 配置中验证。
6. 对第三方明确 CAPTCHA/WAF/登录的站点，系统保留人工恢复，不自动破解。

因此，网页版 AI 如果看到报告中有 `PARTIAL`、`HUMAN_REQUIRED` 或 `ROBOTS_DENIED`，应将其理解为安全状态和真实外部限制，而不是自动判定 Website 代码失败。

## 7. 建议网页版的核验顺序

建议网页版 AI 按下面顺序检查当前 `main`：

1. 先确认 commit `1ef780f`，不要以旧 ZIP 或旧报告作为代码基线。
2. 阅读 `AGENTS.md`，确认 Website 与 Skills 分离，不能导入或读取私有 Skills。
3. 检查 `SiteCategorySnapshot`、`scope_urls`、`scope_cursors` 和稳定 `category_id` 是否真正贯穿 API → Runtime → Pipeline。
4. 检查 `RobotsDenied`、`BrowserHumanRequired`、`BrowserRuntimeMissing` 的异常路径是否仍有清晰状态。
5. 检查 `workers/production_pipeline.py` 的 dimension chain、quota allocation 和 Blender QA。
6. 运行 Python 全量、frontend typecheck/lint/build 和 public tree scan。
7. 用 deterministic Local E2E 验证 21 → 20 + 1 delivery。
8. 再用公司真实配置做一个普通零售站 Exact-1 smoke，不要一上来对 44 站做付费建模。

## 8. 最终反馈

本次 V2 修改已经完成代码级推送，并且核心回归全部通过。当前版本已经适合拿到公司电脑做“真实 Brain + Lux3D + Blender”的受控试点。

但如果验收标准是“任意网站都能无人工、无外部阻断、无配置差异地自动完成建模”，目前还不能诚实地宣称已经达到；公开站点访问控制、真实 API 激活、独立 Vision receipt、Blender 环境和 Holdout 深度覆盖仍需继续验证。

最重要的改进是：现在遇到这些外部或证据问题时，Website 会停在可解释、可恢复、不会伪造成功的状态，而不是显示错误数量、错误 shortage、错误 HUMAN 或错误交付。
