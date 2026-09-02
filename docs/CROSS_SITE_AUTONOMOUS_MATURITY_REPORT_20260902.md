# Furniture Workflow Website｜最终跨网站自治成熟度报告 V2

日期：2026-09-02  
仓库：`eksn425-del/furniture-workflow-website`  
基线：`bda39ea`（September 2 website handoff）  
范围：Website 控制面、通用采集与生产运行时、前端、测试和公开文档；不包含 Skills 包、真实凭据、公司材料、个人数据、浏览器 Profile 或运行产物。

## 1. 最终判定

| 层级 | 结论 |
| --- | --- |
| Website 控制面、状态机、恢复、证据 Gate、交付代码 | `COMPANY LARGE-SCALE PILOT READY` |
| 本机真实外部 Brain / Vision / Lux3D / Blender 激活 | `ACTIVATION GATED`：本轮 Provider OFF，未产生真实付费建模请求 |
| 跨站零样本成熟度证据 | `PARTIAL`：通用核心已能覆盖多种结构并诚实阻断，但 Holdout 尚未达到全量 S2 |

因此，源码可以进入公司电脑的受控大规模试验；不能把本机 Fake Provider/Fake Blender 或单个网站结果表述成所有外部站点和真实模型服务均已完成验收。

## 2. 执行顺序与安全边界

本轮先核对当前 `main`、`AGENTS.md`、README、handoff 和历史报告，再冻结 Holdout 名单，随后执行：

1. Python/Website baseline；
2. 通用类目、数量、Scope、分页和访问路由修复；
3. Product Identity、图片绑定、Local Agent receipt、尺寸和命名 Gate；
4. 任务快照、配额、spillover、队列和终态收敛；
5. GLB bbox、uniform scale、Blender QA 和交付；
6. Website 页面回归、真实公开站点 bounded S0/S1/S2；
7. 全量测试、前端构建、敏感信息扫描和 diff 审查。

第三方站点遇到 robots、登录、WAF、CAPTCHA 或明确人工验证时，没有自动破解，也没有复制用户个人浏览器 Cookie/Profile；保留可恢复状态并分类为 `ROBOTS_DENIED`、`BROWSER_REQUIRED` 或 `HUMAN_REQUIRED`。

## 3. Holdout 冻结记录

开发阶段开始前已记录并冻结：
`Arhaus`、`Nathan James`、`Kayu`、`Indian Hub`、`Archive3D`、`GrabCAD`、`NASA 3D`、`Safavieh`。

名单和规则见 [`CROSS_SITE_HOLDOUT_REGISTER_20260902.md`](CROSS_SITE_HOLDOUT_REGISTER_20260902.md)。本轮没有为这些站点增加 host-specific selector、adapter 或 fixture；结果用于判断 Generic Core 的真实泛化能力。

## 4. 本轮完成的通用修复

### 4.1 类目、数量和 Scope

- 站点扫描先保留原生类目和 scope URL，再生成粗粒度 UI 类目；粗粒度类目被选择时不会只落到一个代表子类目。
- `SiteCategorySnapshot` 使用 `(snapshot_id, category_id)` 复合唯一身份；Job 使用创建时快照，站点重新扫描不会漂移旧 Job 的 Scope。
- `category_quotas`、`PER_CATEGORY`、`TOTAL_ACROSS_SELECTED`、`EVEN`、`PROPORTIONAL`、`CUSTOM` 都以稳定 `category_id` 为键；前端编辑后真正保存策略，不再是只显示的假 UI。
- `UNKNOWN` 不回写成 0。无法访问、尚未扫描、正在扫描和真实空结果保持不同状态。

### 4.2 访问路由与恢复

- robots 明确拒绝时保持 `ROBOTS_DENIED`，不升级成 Browser，也不把失败误判成 CAPTCHA。
- 401/403/405/429/430、可重试服务端错误和 JS shell 只在证据允许时进入同一 L2 Browser 路径。
- L2/人工验证会保留 Job、scan、browser checkpoint；重新恢复使用同一 Job，不创建重复任务。
- Browser 临时失败不会自动降级为 `AI_ESTIMATED` 尺寸；官方字段访问被阻断时进入 `DIMENSIONS_BROWSER_HUMAN_REQUIRED`。

### 4.3 采集、去重和分页

- acquisition checkpoint 升级为 v2，分别记录 `visited_scopes`、`exhausted_scopes` 和每个 Scope 的 cursor。
- 访问过但没有可验证 continuation 的页面不再标成 exhausted，避免 false `TARGET_SHORTAGE`；状态为 `PAGINATION_UNVERIFIED` 时不会伪造 Exact-N 成功。
- 支持显式 `rel=next`、下一页标记、query/page continuation、禁用下一页和 Magento GraphQL `page_info/total_count`。
- 同一 Scope 的 cursor、已看页面、页数和终止原因持久化；形成可扩展 `ScopeCursor`/pagination strategy，而不是 Interior Define 专用补丁。

### 4.4 Product Identity、Vision 和 Production Gate

- 生产配额、候选 lineage 和快照均优先使用稳定 Scope ID，不用容易重名的展示名称。
- Product Identity、Main Product Binding、source/image/vision 关系继续由 receipt 和 lineage 约束；Set/Bundle、场景图、非家具和第三方内容不能为了凑数绕过 Gate。
- 本机明确使用 `LOCAL_AGENT` 作为离线多模态 Reviewer；未配置正式模型时不会发起远程 Brain 请求。
- `TEXT_BRAIN_PLUS_VISION` 的正式配置契约保留，但本轮没有独立公司 Vision 服务可实连，因此没有假装生成独立 Vision receipt。单一多模态模型或 Local Agent 是本轮 Beta 验证模式。

### 4.5 尺寸、Blender 和交付

尺寸来源链保持：

```text
OFFICIAL_STRUCTURED
→ OFFICIAL_PAGE / L2
→ AI_ESTIMATED（仅官网缺少官方尺寸时）
→ target dimensions
```

GLB 处理链已补齐：

```text
raw GLB → POSITION bbox → target dimensions → 优先 uniform scale
→ Blender apply transform → export → final bbox → tolerance QA
```

需要明显非均匀变形时进入 `MODEL_DIMENSION_CONFLICT`，不会硬拉伸后伪报 PASS。Provider OFF 时页面显示能力未启用/下一步，不显示成生产失败。

## 5. Website 页面和真实 UI 回归

本地 Website 实际打开并复核了：

| 页面 | 结果 |
| --- | --- |
| Site Library `/sites` | PASS |
| Site Detail | PASS |
| New Job `/jobs/new` | PASS |
| Job Detail `/jobs/{jobId}` | PASS |
| Review `/review` | PASS |
| Delivery `/delivery` | PASS |
| Runtime `/system` | PASS |

页面刷新后 Job、候选池、状态、事件和阻断原因仍一致；没有发现多 Job selection 串线或多页面 polling 覆盖终态的问题。

### Interior Define canary

Interior Define 本轮只作为 canary：

- Website UI 的 Bedroom Exact-3 Job 显示源站快照数量 186、已发现 `unique 9`，不是错误显示 0；
- Local Agent 复核已记录并可恢复同一 Job；
- 当前真实页面在尺寸 L2 阶段被安全地暂停为 `HUMAN_REQUIRED / DIMENSIONS_BROWSER_HUMAN_REQUIRED`，因为公开商品页当前是 JS 应用壳，官方尺寸没有通过稳定的 L1/L2 证据取得；
- Provider calls 为 0，未伪造模型、尺寸或交付；
- 通用 acquisition 独立探针在 Bedroom 作用域取得：Exact-3 = 3、Exact-25 = 25、Exact-50 = 50；
- Snapshot A → Job A → Rescan B → Resume A 由回归测试验证，旧 Job 不会跟随新扫描漂移。

这表示 Interior Define 的分页/快照能力通过，但本机当前外部页面的尺寸人工核对仍是正式生产前的真实阻断。

## 6. Development Set 结果

Development Set 覆盖 Retail、Direct Brand、High-friction、Marketplace、Design Catalog 和 Asset Repository 等不同结构。S1 是 bounded taxonomy，不要求所有站都成功：

| 站点 | S1 状态 | 类目数 | 数量证据 | 备注 |
| --- | --- | ---: | --- | --- |
| Interior Define | READY | 36 | 公开类目可计数 | 仅 canary |
| Article | PARTIAL | 8 | EXACT 5 / ESTIMATED 1 / UNKNOWN 2 | 结构混合，严格保留不确定性 |
| Castlery | PARTIAL | 27 | EXACT 13 / ESTIMATED 13 / UNKNOWN 7 | 动态零售结构 |
| Anthropologie | ROBOTS_DENIED | — | — | 不绕过 robots |
| Room & Board | PARTIAL | 67 | EXACT 1 / ESTIMATED 9 / UNKNOWN 57 | 高摩擦压力测试 |
| West Elm | PARTIAL | 13 | UNKNOWN | 保留可恢复状态 |
| CGTrader | PARTIAL | 26 | EXACT 2 / ESTIMATED 3 / UNKNOWN 21 | Marketplace，保留既有筛选能力 |
| DesignConnected | PARTIAL | 22 | EXACT 20 / UNKNOWN 2 | 设计目录 |
| Poly Haven | PARTIAL | 14 | ESTIMATED 1 / UNKNOWN 13 | 资源目录，不把素材误当家具 |
| Alessi | READY | 15 | EXACT 1 / ESTIMATED 14 | 零售目录 |
| Fabuliv | PARTIAL | 28 | EXACT 20 / UNKNOWN 8 | 区域零售 |

统计：11 个 Development Site 中 `READY=2`、`PARTIAL=8`、`ROBOTS_DENIED=1`。这些状态是证据结果，不是为了通过而把 UNKNOWN 变成成功。

## 7. 45 站 bounded S0 sweep

完成 44 个历史站点加 West Elm 的 bounded S0：

| 状态 | 数量 | 含义 |
| --- | ---: | --- |
| `READY` | 30 | 入口取得可继续的公开证据 |
| `BROWSER_REQUIRED` | 4 | 普通 HTTP 不足，需同一 L2 会话 |
| `ROBOTS_DENIED` | 6 | robots 明确拒绝 |
| `FAILED` | 5 | 当前网络/域名/入口失败，保留证据 |
| 合计 | 45 | bounded S0 完成 |

S0 不等于商品生产成功，也不等于站点已授权绕过访问控制。代表性分类包括：Anthropologie/RH/GrabCAD/Free3D/NASA 3D/Safavieh 为 robots；Arhaus/Ligne Roset/Archibase/3DExport 需要 Browser，其中 3DExport 有明确 CAPTCHA；Iris/LoveNSpire/Archive3D/Archibase/3DXO 等部分入口为网络策略或域名失败。

## 8. Holdout 的第一次零样本结果

| Holdout | S0 | 第一次深探针 | 结论 |
| --- | --- | --- | --- |
| Arhaus | `BROWSER_REQUIRED` | 未自动升级/不绕过 | HTTP 430，需人工/可见会话 |
| Nathan James | `READY` | `Exact-1 READY` | 无专用规则完成 S1 + 一个候选发现 |
| Kayu | `READY` | 外部网络 `Invalid IPv6 URL` | 保留为外部网络阻断，不伪造失败产品 |
| Indian Hub | `READY` | 未继续生产 | 可进入下一次 S2 |
| Archive3D | `FAILED` | 未继续生产 | 网络策略错误 |
| GrabCAD | `ROBOTS_DENIED` | 未继续生产 | robots policy |
| NASA 3D | `ROBOTS_DENIED` | 未继续生产 | robots policy |
| Safavieh | `ROBOTS_DENIED` | 未继续生产 | robots policy |

Holdout 证据达到了“通用核心能正确分类、至少一个未专门适配站点完成 Exact-1”的要求，但没有达到所有 Holdout 的 S2；这是当前成熟度仍为 `PARTIAL` 的主要原因。

## 9. S2 bounded 结果摘要

除了 Nathan James 的 Exact-1 READY 外，其他探针诚实返回：

- Article、CGTrader、Poly Haven：`ProductSupplyExhausted` 或 Scope 没有可确认的剩余唯一商品；
- DesignConnected、Alessi：`PAGINATION_UNVERIFIED`，不能把访问过的第一页当作完整供应；
- Kayu：外部网络解析错误；
- Room & Board / Interior Define：可到 L2/人工或尺寸验证阶段，保留 Job checkpoint；
- Fabuliv、部分高摩擦站点：访问验证时进入 `HUMAN_REQUIRED`，不破解。

这组结果验证了“缺货、分页未知、外部阻断、人工验证、可恢复 Job”之间的状态区别，但不应被报告为全部站点已建模。

## 10. Deterministic full E2E

`services/api/tests/test_website_e2e.py` 通过完整的本地闭环：

```text
Local mock site
→ taxonomy EXACT=21
→ Exact-21 Job / immutable scope
→ discovery / identity / media / Local Agent / dimensions / naming Gate
→ Fake Lux3D（provider_concurrency=5）
→ Fake Blender QA
→ COMPLETED/SUCCEEDED
→ delivery ZIP 20 + 1
```

断言包括 21 个 Provider ledger、21 个 Blender QA PASS、所有名称 ≤50 字符、manifest 的官方尺寸来源和 `DELIVERY_BATCH_ZIP + MANIFEST_JSON`。Fake Provider 只在显式 `FURNITURE_WORKFLOW_LOCAL_E2E=1` 和 `LOCAL_E2E` profile 下启用，普通生产不会隐式切换。

## 11. 测试与发布前门禁

| 检查 | 结果 |
| --- | --- |
| Python/API/workflow 全量 | PASS：130 tests |
| Website E2E + Blender targeted | PASS：10 tests |
| Python `compileall` | PASS |
| Web TypeScript | PASS |
| Web ESLint | PASS：0 errors；保留 1 个既有 `no-img-element` warning |
| Next.js production build | PASS |
| `npm audit --omit=dev --audit-level=high` | PASS：0 vulnerabilities |
| `scripts/check_public_tree.ps1` | PASS：132 个文本文件 |
| 敏感信息/公开树扫描 | PASS：未发现真实 key、私有运行时文件或凭据 |
| `git diff --check` | PASS；仅有 Windows 换行提示 |

新增/修改的 taxonomy、pagination、scope snapshot、quota、dimension challenge、Blender QA 和 convergence 均有 focused regression；没有只靠这次人工点击作为唯一证据。

## 12. 过拟合审计

- 新增 host-specific if/else：`0`
- 新增 Holdout 专用 Site Adapter：`0`
- 新增 Generic Core 能力：Scope URL/immutable snapshot、durable cursor/pagination、稳定配额、访问分类、尺寸人工阻断、GLB dimension QA 等通用能力
- Interior Define 仅保留 canary，不以它的路径为全局成功门槛。

当前跨站失败主要是公开站点策略、robots、动态结构、分页证据不足或外部网络状态，未通过硬编码站点分支掩盖。

## 13. 公司电脑下一步

1. 注入公司内部 Brain/Vision、Lux3D 和 Blender 的本地配置，不把值写入仓库、截图、日志或工单。
2. 先用 1 个普通 Retail 和 1 个 Marketplace 做真实 `Exact-1` smoke。
3. 检查真实 Brain 的 `reviewed_media_sha256`、官方尺寸来源、模型任务 id、幂等恢复和最终 bbox。
4. 确认一个真实模型到 Blender 打开、尺寸 QA 和 Delivery 下载后，再扩大并发与站点数量。
5. 高摩擦站点继续由操作员完成人工验证；验证完成后恢复原 Job，不创建副本。

最终建议：源码可以进入受控公司大规模试点；“任意网站全部零样本自动完成”和“真实 Lux3D/Blender 全站点生产”仍需公司环境的真实服务与站点授权继续验收。
