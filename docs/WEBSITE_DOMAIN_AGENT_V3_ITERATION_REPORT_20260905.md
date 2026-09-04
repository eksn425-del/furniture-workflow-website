# Furniture Workflow Website — Domain Agent / Bounded Autonomy V3

日期：2026-09-05
仓库：`eksn425-del/furniture-workflow-website`
GitHub：`https://github.com/eksn425-del/furniture-workflow-website`
起始分支：`codex-v3-iteration-20260904`
`STARTING_COMMIT=7d6e098e9623f907df828b05d7a00cbad2ba4821`
`FINAL_COMMIT=本报告所在的本地提交（提交标题：Evolve website into bounded domain-agent workflow；精确 SHA 以提交后的 git rev-parse HEAD 为准）`

## 1. 范围、来源与发布边界

本轮完整阅读了用户提供的 V3 执行文档（1027 行）、仓库 `AGENTS.md`、README/架构文档、当前 `origin/main` 和相关 Runtime/Worker/测试代码。先执行了 `git fetch origin main --prune`，确认本轮基线是 GitHub 当前真实 `origin/main` 的 `7d6e098`，再从该基线建立本地迭代分支。

用户明确要求本轮修复后由本人验收再提交 GitHub，因此本轮只保留本地 branch/commit，**没有执行 `git push`，也没有覆盖 GitHub 上的版本**。附带 Prompt 中要求 push 的文字被用户最新请求覆盖。

本轮没有读取、导入、复制或运行项目的私有 Skills checkout，没有创建第二套 Agent Engine；所有能力都落在 Website 自己的 Contract、Generic Core、Durable State、Receipt 和现有 Runtime 上。

## 2. 审计结论

起始版本的主流程、`workflow-event.v2`、Site/Job/Snapshot、Provider Ledger、Hard Gate、Generic Core 和交付收据架构均保留。审计识别出以下 V3 风险：

- Agent 主要停留在 taxonomy，缺少可审计的策略选择、商品/图片/分页/尺寸工具闭环；
- 站点知识缺少可复用、可漂移检测的 SiteProfile 数据契约；
- 画像选择、尺寸来源和 AI 估算边界仍容易被普通回退路径混淆；
- 省略 Provider 审批额度时会落到过大的默认值，不符合 Exact-N 成本边界；
- 生产页面需要把技术状态折叠成普通员工可操作的状态和动作；
- Article 等新站点的 L2 导航失败必须保留 checkpoint，不能伪装成“无类目”或成功。

本轮没有为了让某个站点变绿而增加 host-specific adapter；新增能力均为通用策略、证据和状态治理。

## 3. 本轮实现

### 3.1 Strategy Catalog

新增 `services/api/app/services/strategy_catalog.py`，以数据方式登记 taxonomy、product discovery、pagination、PDP、image、dimensions 六类受控策略。平台只用于生成首选计划，Agent 不能提交未登记策略；验证失败返回 `UNSUPPORTED_STRATEGY_REQUIRED`。控制面新增 `/api/v1/control/strategy-catalog` 诊断入口。

### 3.2 SiteProfile v1

新增 `services/api/app/services/site_profile.py` 和 `SiteProfileContract`：

- 保存站点、平台、六类策略、公开 URL pattern、safe hints、证据、置信度、状态和 profile version；
- `DRAFT / VALIDATED / STALE / BLOCKED` 状态分离“学习中、已验证、需要重学、访问阻断”；
- 只接受公开 HTTP(S) 来源；递归拒绝 cookie、token、password、session 等秘密或会话字段；
- 复用前验证 site key、platform 和策略漂移；
- blocked/offline receipt 也有自描述 profile，但绝不被当成可复用生产真相；
- 扫描 receipt、SiteProfile 数据库行、Production contract 和 Product Acquisition 共享同一 v1 数据。

### 3.3 Site Intelligence Agent 与受控工具

在现有 Brain Agent Loop 上扩展以下 bounded 工具：`list_categories`、`discover_products`、`inspect_product`、`inspect_pagination`、`list_product_images`、`inspect_dimensions`、`validate_strategy`、`finish_site_profile`。

- 工具只能访问同一公开站点，跨站 URL 返回终态工具错误；
- 每个工具有显式上限、结构化结果摘要和 evidence hash；不执行 Agent 自定义代码或任意请求头；
- `finish_site_profile` 只有在所有选中策略都由工具产生证据且显式 `validate_strategy` 通过时，才可标记 `VALIDATED`；否则为 `DRAFT`；
- receipt 保存 `agent_run_id`、task、steps/tool calls、结果摘要、stop code、selected strategies、profile version 和 evidence hash；不保存 private chain-of-thought；
- Agent Loop 明确区分 `BRAIN_NOT_CONFIGURED`、schema 无效、tool budget、终态工具错误和恢复动作。

### 3.4 Generic Core 优先与 SiteProfile 复用

`ProductAcquisitionEngine` 先走已有通用 JSON-LD、Shopify/Magento 公开结构、sitemap、HTML card 和 bounded browser 路径；只有验证过的 SiteProfile 才能提供已审计的策略 cursor。Profile 缺失、DRAFT、STALE 或 BLOCKED 时重新走证据学习，不会把旧 profile 静默当成生产真相。

### 3.5 图片策略

新增严格的单一产品图排序与选择：优先 JSON-LD/产品画廊/OG 等产品证据，排除 lifestyle、room scene、swatch、placeholder、sprite 和明显非产品图。AI 只能提出建议；系统保留选图 URL、来源角色、接受/拒绝原因和 hash，生产 Gate 才决定合格与否。

### 3.6 命名 Contract

所有最终名称统一按整词治理，字符数包含空格并严格 `<= 50`。Direct Brand 保留官方名优先；Marketplace/低权威来源使用品牌、官方名、风格/颜色/材质/类型的可审计组合。旧的 140 字符例外已移除；兼容旧测试的 `SOURCE_NAME_FIRST` 只作为历史 decision alias，实际治理来源另存为 `naming_governance`。

### 3.7 尺寸来源 Contract

尺寸阶段现在严格按：

`official structured/page → bounded L2 browser → OFFICIAL_ABSENT_CONFIRMED 后才允许 AI`

官方三轴完整时不运行 L2；可见 CAPTCHA、临时故障、访问拒绝和官方数据不完整都进入可恢复的 `PENDING`，不会静默 AI 补齐。只有显式 `allow_ai_dimension_override`/授权字段才允许覆盖，并记录 `AI_ESTIMATED_OVERRIDE`、lookup state、单位和 provenance。尺寸不访问到不等于 0。

### 3.8 Provider 安全与耐久状态

- Exact-N 且未填写审批额度时，默认额度为请求目标 N（`ALL` 至少需要显式 1）；PER_CATEGORY 按选中类目乘算；
- `CREATE_IN_FLIGHT`、`ACTIVE`、`SUBMISSION_UNKNOWN` 和已确认 Provider task 占用调用额度；
- `SUBMISSION_UNKNOWN` 永不自动重复付费 POST；同一 idempotency key 只恢复已知 task 或进入人工对账；
- capacity wait 不把无 task ID 的瞬时容量拒绝误当成已计费调用，fixture 用例跳过长生产等待，真实运行仍保留 bounded wait；
- Provider OFF 保持默认，所有本地测试均没有真实付费 Provider POST。

### 3.9 Runtime、API 与员工 UX

Site Scan 会加载/持久化 SiteProfile，Production Runtime 将同一 profile 注入 acquisition contract。控制面提供安全 profile read model；恶意或损坏 profile 返回有限的 INVALID envelope，不回显秘密字段。前端新增 profile/strategy/dimension/agent 状态标签、Exact-N 文案和折叠技术详情，只有真实 action-needed 状态打断普通员工。

## 4. 按 Prompt 要求的 14 个明确回答

1. **Website 多了什么 Agent 能力？**  从只做 taxonomy 扩展为 Site Intelligence Agent：可在同站 bounded 探索类目、商品、分页、PDP、图片和尺寸，选择受控策略，验证并产出 SiteProfile。
2. **新网站第一次怎么探索？**  URL 预检 → robots/公开 HTML/sitemap/L0-L1 → 通用导航/结构化数据 → 必要时同一可见 L2 → Agent 在工具预算内补证据；每一步写入 snapshot/receipt/checkpoint。
3. **SiteProfile 如何学习、验证和复用？**  Agent 根据公开证据选择 Catalog 策略；工具结果和 `validate_strategy` 验证后才 `VALIDATED`。后续按 site key/platform/策略漂移复用；缺失、失效或阻断则 DRAFT/STALE 并重学。
4. **如何避免 Skills 时代忘记 SOP？**  SOP 被迁移为 Python Contract、Strategy Catalog、Hard Gate、Durable Receipt、状态机和 focused regression，而不是依赖上下文记忆或外部 Skills 文件。
5. **如何避免 CGTrader 时代限制太死？**  Agent 只在受控 Catalog 内选策略，Generic Core 先行，允许多种公开结构、bounded browser 和显式恢复；不再为每个站点写大量 host 分支，也不把单一失败当成全局失败。
6. **AI 的自主权？**  GREEN 区可选择探索顺序、策略、类目归并、产品候选和图片建议；YELLOW 区可提出尺寸/命名/视觉判断，但必须经过 Contract 和证据校验。
7. **AI 绝对没有最终权力？**  不能绕过 robots/CAPTCHA/WAF/login，不能读取秘密/复制 Cookie，不能越站，不能伪造数量/尺寸/图片合格，不能突破 Exact-N、Provider 额度、idempotency 或交付 Gate，也不能把 UNKNOWN 写成 0。
8. **命名是否严格符合 Contract？**  是，当前最终名称统一整词治理且 `<=50`；来源、字段、字符数和 Gate 结果留在 candidate lineage。
9. **尺寸是否官方优先？**  是。官方完整值直接接受；否则先 bounded L2；只在明确官方缺失或显式授权 override 后才 AI 估算，并标记来源和状态。
10. **图片是否优先干净单一产品图？**  是。排序器优先结构化产品/画廊媒体，拒绝 lifestyle、swatch、placeholder 等；AI 只建议，系统 Gate 决定。
11. **Provider 是否安全？**  是。默认 OFF、Exact-N 默认额度、POST 前 hard limit、未知提交隔离、同 key 恢复、无真实付费本地批量测试。
12. **全量测试结果？**  后端/API/workflow 共收集 162 个测试全部通过；V3 focused tests 6/6 通过；Python touched-file `py_compile` 通过；Web typecheck、lint、production build 通过；公开树扫描 `FILES_SCANNED=141` 通过。
13. **Live smoke 结果？**  本地可见浏览器的 Home/New Job/Sites/Jobs/Review/Delivery/System/Job Detail 均可加载，API/DB/L2/Local Agent 状态来自真实运行时。Provider POST=0。6 个代表性公开站点的 bounded 结果见第 5 节；外部 robots、超时和导航阻断被如实保留。
14. **还必须公司真实环境验证什么？**  公司网络/DNS/代理、Playwright Chromium 和持久 Profile、真实 Website Brain/Vision receipt、Lux3D/Provider 额度与幂等、Blender 可执行路径、对象存储、真实模型下载/GLB QA、公司权限和条款验收，均不能由本地 OFF/LOCAL_AGENT 证据替代。

## 5. 测试与 Live Smoke 证据

### 5.1 自动化回归

| 检查 | 结果 |
| --- | --- |
| `pytest -q services/api/tests/test_domain_agent_v3.py` | PASS：6/6 |
| `pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests --maxfail=1` | PASS：162/162 |
| touched Python `py_compile` | PASS |
| `npm run typecheck` | PASS |
| `npm run lint` | PASS，0 errors；保留 2 个既有 warning（未使用 `useRef`、Next `<img>` 性能提示） |
| `npm run build` | PASS；Next 16.3.0 production routes 生成 |
| `git diff --check` | PASS；仅 Windows 换行提示 |
| `scripts/check_public_tree.ps1` | PASS：141 个文件；无真实 key/凭据运行时文件 |

### 5.2 本地可见 Web/API smoke

- Home 显示 `API 在线`、`Provider OFF`；
- New Job 四步向导可以输入 Article URL 并创建持久 scan/job；
- Jobs 列表读取 34 个历史 Job，最新 Article Job 显示 `等待复核`；
- Sites 页面显示站点档案和真实阻断原因；Article 当前为临时故障，不把旧类目冒充已验证；
- Review 页面正确显示当前无待处理动作；Delivery 页面只列出已有 `DELIVERED` artifact；
- System 页面显示 `Database READY`、`L2 Browser READY / chromium / ISOLATED_PERSISTENT`、`Website Brain LOCAL_AGENT`、`Provider OFF_BY_DEFAULT`、receipt-gated modeling/QA worker；
- Article 的可见 Job Detail 显示 `BROWSER_NAVIGATION_FAILED`、可恢复提示、类目未生成和 Provider calls=0；Live Activity 不展示模型内部推理。

### 5.3 6 站 bounded live smoke（Provider OFF）

运行时使用同一 SafeHttpClient、每站最多 8 个公开请求、请求 timeout 4 秒、无请求延迟；结果只表示本次环境的入口证据，不等于商品生产成功。

| 站点 | 本次状态 | 证据/阻断 |
| --- | --- | --- |
| Alessi | `READY` | 318,904 bytes；4 requests；2 redirects |
| Fabuliv | `READY` | 666,860 bytes；2 requests |
| Castlery | `FAILED` | `READTIMEOUT`，保留为外部网络失败 |
| Article | `ROBOTS_DENIED` | robots 规则拒绝当前入口，不绕过 |
| Muji | `ROBOTS_DENIED` | robots 规则拒绝当前入口，不绕过 |
| Interior Define | `ROBOTS_DENIED` | robots 规则拒绝当前入口，不绕过 |

同一开发环境在前一轮短探针中曾观察到 Castlery/Article/Interior Define 可达、Muji 需浏览器；外部 robots、DNS、网络和页面策略会变化，因此报告以每次运行的原始 blocker 为准，不把波动归因成代码回归。历史 45 站 bounded S0 证据仍保留在 `docs/CROSS_SITE_AUTONOMOUS_MATURITY_REPORT_20260902.md`（READY 30、BROWSER_REQUIRED 4、ROBOTS_DENIED 6、FAILED 5），本轮遵守 V3 Prompt §20，没有再次发起长时间 45 站 campaign。

## 6. 剩余阻断与验收建议

当前结论是：**本地代码与受控回归已达到可交付验收状态；真实公司环境仍为 PARTIAL / REQUIRES COMPANY VALIDATION。**

仍需公司验收的红区包括真实 Brain/Vision/Lux3D/Blender/对象存储、外部站点访问授权、公开站点条款、模型生成与下载、20 个一批交付和真实故障恢复。Anthropologie/Article/Muji 等站点的 robots、CAPTCHA 或临时导航失败必须由公司运营人员决定是否提供官方导出、调整网络或人工完成验证；系统不会绕过这些控制。

本轮只做了本地 commit，没有 push。验收通过后由用户自行检查 diff、运行公司环境测试并决定是否推送。
