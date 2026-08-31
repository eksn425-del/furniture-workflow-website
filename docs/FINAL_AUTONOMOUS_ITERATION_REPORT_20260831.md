# Furniture Workflow Website｜Codex 最终自主迭代报告

日期：2026-08-31
仓库：`eksn425-del/furniture-workflow-website`
范围：Website 源码、测试、配置模板和公开文档；不包含 Skills 包、真实凭据、公司材料、个人数据、浏览器 Profile 或运行产物。

## 1. 最终结论

本轮完成了从 baseline、源码修复、真实 Website UI 回归、44 站 bounded S0、8 站 S1 taxonomy、8 次不同站点 S2 尝试、确定性 21→20+1 E2E、前端构建和安全审计到发布前检查的完整闭环。

结论分两层表达：

| 层级 | 结论 |
| --- | --- |
| Website 控制面与工作流代码 | `COMPANY LARGE-SCALE PILOT READY`（可进入公司环境激活测试） |
| 本机真实生产集成 | `ACTIVATION GATED`：本机保持 Local Agent、Provider OFF，未安装/启用真实 Blender，也未调用真实 Brain、Vision 或 Lux3D |

这意味着 Website 的状态机、证据链、恢复机制、生产 Gate 和交付链路已经可以承接公司大规模试验；并不把本地 Fake Provider/Fake Blender 结果冒充为公司真实服务已经连通。公司电脑注入正式配置后，必须再做一次小规模 live smoke 和真实模型验收。

## 2. Baseline 与本轮执行顺序

先执行了原有后端测试、前端 typecheck/lint/build、公开树扫描和 diff 检查，确认修改前基线可运行。随后按执行文档顺序完成：

1. 运行时边界和同源 API 代理修复；
2. 类目/数量、L1-first/L2-on-demand、访问状态和恢复路径修复；
3. Product Identity、图片与视觉 receipt、尺寸来源和命名 Gate 检查；
4. Blender bbox、uniform scale、apply transform、最终 bbox 与 tolerance QA；
5. Website UI 操作、刷新、多路由、Job 持久化和 Provider OFF 状态回归；
6. 44 站 S0、8 站 S1、8 次 S2 真实 bounded 尝试；
7. 全量 Python、前端、依赖审计、公开树和敏感信息检查；
8. 生成本报告并准备提交到 GitHub `main`。

## 3. 已完成的最小修复

| 区域 | 修复结果 |
| --- | --- |
| API 同源访问 | Website 默认使用 `/api/v1`，由 Next 代理到本地控制面；仍支持公司部署显式覆盖，不再把 `127.0.0.1:8000` 固化到浏览器 bundle。 |
| Brain 模式 | 无正式 Brain 配置时明确使用 `LOCAL_AGENT`；显式配置可选择 `MULTIMODAL_SINGLE_MODEL` 或 `TEXT_BRAIN_PLUS_VISION`；没有凭据时不伪造远端 READY。 |
| Runtime Diagnostics | `/control/system` 显示实际 API、DB、L2 Browser、Brain、override、Lux3D、Blender、队列和 Worker 状态；不显示 key、私有 URL、会话目录或本机路径。 |
| 访问分类 | `ROBOTS_DENIED` 不再被误报为 `HUMAN_REQUIRED`；401/403/405/429/430 和可重试服务端错误按证据升级到同一 L2 浏览器路径；明确 CAPTCHA/验证控件才进入人工状态。 |
| 类目与数量 | 扫描失败、进行中、机器人拒绝和未知数量继续使用真实状态，不回写为虚假的 `0`；保留历史快照、失败证据和可恢复入口。 |
| 商品采集 | Article 类目真实返回 HTTP 405 时，旧代码会直接失败；现在按 L2 访问证据处理并保留同一 Job。严格视觉复核拒绝场景图、Set/Bundle 和非家具，不为凑 Exact-N 放宽 Gate。 |
| 尺寸链 | 补齐 `OFFICIAL_STRUCTURED → OFFICIAL_PAGE/L2 → AI_ESTIMATED → target dimensions`；官网没有尺寸时才允许明确标记 `AI_ESTIMATED`。 |
| Blender | 解析 GLB POSITION bbox，按目标尺寸优先 uniform scale，执行 Blender import/transform apply/export，检查最终 bbox 和 15% tolerance；需要明显非均匀变形时进入 `MODEL_DIMENSION_CONFLICT`。 |
| Ready Pool | Provider OFF 时不再把安全闸门显示成红色失败；运行卡片和安全摘要均显示 `Ready Pool 已保存` / `下一步`，提示选择并审批 Provider。 |
| 交付 | 本地 E2E 继续验证 20+1 批次 ZIP、manifest、GLB 数量、尺寸来源和 Blender QA 结果；Ready Pool 不会冒充已生成 GLB。 |

## 4. Website 真实 UI 回归

使用可见 Website 浏览器完成了实际页面导航和刷新，不读取用户日常浏览器 Cookie、密码、历史或个人 Profile。

### 4.1 核心页面

以下路由均实际打开并确认主内容与标题存在：

| 路由 | 结果 |
| --- | --- |
| `/sites` | PASS：网站库、站点状态和扫描入口可见 |
| `/jobs` | PASS：任务列表可见 |
| `/review` | PASS：人工处理中心可见 |
| `/delivery` | PASS：交付下载页可见 |
| `/system` | PASS：系统健康页可见 |
| `/jobs/{jobId}` | PASS：已有 Job 刷新后仍能恢复候选池、命名和 Gate 状态 |

### 4.2 Interior Define Local Agent 工作流

真实 Website 操作完成了：加入已公开站点 → 摸底 → 选择 Bedroom/Beds → 保存 Exact-3 目标 → 创建 Job → Website Native discovery → L2 候选图片查看 → Local Agent 视觉复核 → 尺寸/命名/Gate → Ready Pool。

结果：

- 发现 9 个去重候选；
- 3 个候选完成 Local Agent 复核、尺寸写入和最终命名；
- 名称字符数含空格均小于等于 50；
- 候选状态进入 `MODEL_INPUT_LOCKED`，并显示 `review_provider=LOCAL_AGENT`；
- Provider 保持 OFF，没有发生任何真实 Lux3D 调用；
- Job 停在 `PROVIDER_SAFETY`，Ready Pool 保留，刷新后可继续，不被显示为失败。

### 4.3 刷新与状态同步

刷新已有 Job 后确认：

- Job URL 没有跳错；
- Ready Pool、候选名称和 `MODEL_INPUT_LOCKED` 状态仍在；
- Provider 安全提示为 attention/下一步，而不是 danger/失败；
- 轮询不会把已结束的状态重新覆盖成 UNKNOWN；
- 多页面顺序访问不会串用错误的 Job 或 scan 状态。

## 5. 44 站 bounded S0

本轮按历史 44 站点池执行有限预检。S0 只证明入口当前可分类，不承诺每站商品生产成功，也不绕过 robots、WAF、登录或 CAPTCHA。

### 5.1 批次统计

| 状态 | 数量 | 语义 |
| --- | ---: | --- |
| `READY` | 28 | 当前预检取得可继续的公开入口证据 |
| `ROBOTS_DENIED` | 9 | robots.txt 明确拒绝，不等于人机验证 |
| `BROWSER_REQUIRED` | 4 | 普通 HTTP 证据不足，需同一可见浏览器继续；不自动推断存在 CAPTCHA |
| `FAILED` | 3 | 当前网络/域名/入口失败，保留失败证据 |
| 合计 | 44 | bounded S0 完成 |

### 5.2 逐站结果

| # | 站点 | S0 状态 | 备注 |
| ---: | --- | --- | --- |
| 1 | Anthropologie | ROBOTS_DENIED | robots policy |
| 2 | Arhaus | BROWSER_REQUIRED | HTTP_430，升级可见浏览器 |
| 3 | Article | READY | 入口可继续；商品阶段另测到 HTTP 405 并已修复为 L2 路径 |
| 4 | Castlery | READY | 入口可继续 |
| 5 | Interior Define | READY | 入口可继续 |
| 6 | Ligne Roset | BROWSER_REQUIRED | JAVASCRIPT_CHALLENGE |
| 7 | MacKenzie-Childs | READY | 入口可继续 |
| 8 | Nathan James | READY | 入口可继续 |
| 9 | POLYWOOD | READY | 入口可继续 |
| 10 | RH | ROBOTS_DENIED | robots policy |
| 11 | Room & Board | ROBOTS_DENIED（批次） | 独立单次复核随后返回 READY，说明边缘策略具有动态波动 |
| 12 | Rove Concepts | READY | 入口可继续 |
| 13 | Sixpenny | READY | 入口可继续 |
| 14 | Walker Edison | READY | 入口可继续 |
| 15 | India Art n Design | READY | 入口可继续 |
| 16 | Kayu | READY | 入口可继续 |
| 17 | Fabuliv | READY | 入口可继续 |
| 18 | Globally Indian | READY | 入口可继续 |
| 19 | Indikasa | READY | 入口可继续 |
| 20 | Iris | READY | 入口可继续 |
| 21 | Hometown | READY | 入口可继续 |
| 22 | Featherlite | READY | 入口可继续 |
| 23 | Interio | FAILED | NETWORKPOLICYERROR / hostname resolution |
| 24 | Durian | READY | 入口可继续 |
| 25 | Indian Nest | READY | 入口可继续 |
| 26 | Furnishka | READY | 入口可继续 |
| 27 | LoveNspire | READY | 入口可继续 |
| 28 | Indian Hub | ROBOTS_DENIED | robots policy |
| 29 | DesignConnected | READY | 入口可继续 |
| 30 | Archive3D | FAILED | 网络策略错误 |
| 31 | GrabCAD | ROBOTS_DENIED | robots policy |
| 32 | CGTrader | READY | 入口可继续；保留既有筛选策略 |
| 33 | Free3D | ROBOTS_DENIED | robots policy |
| 34 | Archibase | BROWSER_REQUIRED | JAVASCRIPT_CHALLENGE |
| 35 | 3DExport | BROWSER_REQUIRED | 明确 CAPTCHA，需人工处理 |
| 36 | 3D Warehouse | READY | 入口可继续 |
| 37 | Sweet Home 3D | READY | 入口可继续 |
| 38 | NASA 3D | ROBOTS_DENIED | robots policy |
| 39 | 3DXO | FAILED | 网络策略错误 |
| 40 | MyMiniFactory | READY | 入口可继续 |
| 41 | Poly Haven | READY | 入口可继续 |
| 42 | Alessi | READY | 入口可继续 |
| 43 | Safavieh | ROBOTS_DENIED | robots policy |
| 44 | Driade | ROBOTS_DENIED | robots policy |

### 5.3 Room & Board 结论

Room & Board 仍应被当作压力测试，而不是全局通过门槛。本批并发预检中返回过 `ROBOTS_DENIED`，随后单独重试返回 `READY`；历史可见浏览器测试还观察到 Technical difficulties 页面和人工验证后的恢复路径。证据说明：

- 访问并非“永远不可用”；
- 站点边缘策略、会话、频率和时间会影响结果；
- 没有 CAPTCHA 按钮也可能被访问策略挡住；
- 系统现在会保存状态并区分 robots、L2、HUMAN_REQUIRED 和普通 FAILED；
- 本轮没有把 Room & Board 的 14 个 Vanity 重新伪造为全量成功，也没有绕过其访问控制。

## 6. 8 站 S1 taxonomy 回归

对不同结构站点进行了完整的 bounded taxonomy 分析。结果均来自实时公开入口，允许 PARTIAL/UNKNOWN，不为凑通过率补猜数量。

| 站点 | 状态 | 类目数 | 数量证据摘要 | 结构观察 |
| --- | --- | ---: | --- | --- |
| Article | PARTIAL | 8 | EXACT 5 / ESTIMATED 1 / UNKNOWN 2 | 类目页与商品页混合 |
| Interior Define | READY | 36 | 全部 EXACT | 公开类目计数清晰 |
| MacKenzie-Childs | PARTIAL | 37 | EXACT 16 / UNKNOWN 21 | 多入口/Marketplace 信号 |
| POLYWOOD | PARTIAL | 40 | EXACT 4 / UNKNOWN 36 | 类目可见但计数不完整 |
| Fabuliv | PARTIAL | 29 | EXACT 10 / UNKNOWN 19 | 通用电商导航 |
| DesignConnected | PARTIAL | 22 | EXACT 20 / UNKNOWN 2 | 设计目录结构 |
| CGTrader | PARTIAL | 26 | EXACT 2 / ESTIMATED 2 / UNKNOWN 22 | Marketplace；保留既有筛选能力 |
| Poly Haven | PARTIAL | 14 | ESTIMATED 1 / UNKNOWN 13 | 资源/素材目录，家具范围需谨慎 |

## 7. 8 次 S2 bounded 尝试

S2 的目标是验证 Job、Scope、候选发现、证据 Gate、人工恢复和安全停机。结果不强行要求所有站点成功。

| 站点 / Job 类型 | 结果 | 关键证据 |
| --- | --- | --- |
| Interior Define Exact-3 | READY POOL 3 | 完成 Local Agent、尺寸、命名和 `MODEL_INPUT_LOCKED`，Provider OFF 安全暂停 |
| Article Exact-1 | BLOCKED | 真实 HTTP 405 触发 L2 修复；发现 Set/Bundle/场景图候选，严格视觉拒绝 3 个，保留同一池继续审核 |
| Castlery Exact-1 | PARTIAL / NO CANDIDATE | 动态结构下扫描可达但没有可用类目候选 |
| Fabuliv Exact-1 | HUMAN_REQUIRED | L2 浏览器遇到可见访问阻断，保留 checkpoint，不绕过 |
| MacKenzie-Childs Exact-1 | TARGET_SHORTAGE | 选定 Furniture 类目没有剩余唯一公开商品，不降低 Gate |
| CGTrader Exact-1 | ROBOTS_DENIED | robots policy，未伪造商品结果 |
| Poly Haven Exact-1 | PARTIAL / OUT OF SCOPE | 识别到的入口主要是资源插件类目，未把它们当家具商品 |
| Alessi Exact-1 | TARGET_SHORTAGE | 站点扫描成功、15 类目可见；Kitchen Accessories 选区没有剩余唯一公开商品 |

S2 的跨站真实结果是“一个站点完成 Ready Pool，多站点被证据不足、访问策略、结构不支持或目标短缺诚实阻断”。这证明了状态机与 Gate 的行为，但不应被表述为 8 站都已生成模型。

## 8. 尺寸、Vision、Lux3D 和 Blender

### 8.1 尺寸

结构化字段现按以下优先级回写：

```text
OFFICIAL_STRUCTURED
→ OFFICIAL_PAGE / L2 dimensions tab
→ AI_ESTIMATED（只有官网没有官方尺寸时）
→ target_dimensions
```

缺字段不能静默变成官方尺寸；产品 receipt、candidate lineage、model input 和 delivery manifest 都保存尺寸、来源和单位。

### 8.2 Vision

本机本轮实际使用的是 Website Local Agent / Multimodal Reviewer，不调用外部模型。`TEXT_BRAIN_PLUS_VISION` 的正式模式接口仍保留，但本轮没有独立的公司 Vision Provider 可供实连验证，因此没有假装生成独立 Vision receipt。公司 Beta 建议优先使用单一多模态模型或显式 Local Agent，正式部署后再单独验收文本 Brain + 独立 Vision。

### 8.3 Lux3D

本机 Provider 保持 OFF，未产生付费请求。系统只有在公司环境注入完整 Lux3D 配置、通过资格和成本安全闸门后才允许真实建模。API 路由和配置变量保留，真实 key 不进入仓库或报告。

### 8.4 Blender

本地生产诊断如实显示 `Blender NOT_CONFIGURED`。代码已补齐真实 Blender CLI adapter 的导入、uniform normalization、transform apply、导出、最终 bbox 和 tolerance QA；未安装 Blender 的机器不会把 raw GLB 直接标记为已交付。

确定性 Local E2E 以 Fake Lux3D + Fake Blender 验证：

- Exact-21 输入；
- 21 个完成候选与 Provider ledger；
- 21 个 Blender QA `PASS`；
- 交付批次 `20 + 1`；
- ZIP manifest 含 target dimensions、dimension source 和 Blender QA。

## 9. 确定性全链路 E2E

`services/api/tests/test_website_e2e.py` 已实际通过以下链路：

```text
Local mock site
→ preflight
→ taxonomy EXACT=21
→ Exact-21 Job
→ scope binding
→ discovery
→ identity/image/vision/dimension/naming gate
→ 21 Fake Lux3D tasks（provider_concurrency=5）
→ 21 Fake Blender QA
→ COMPLETED/SUCCEEDED
→ delivery ZIP 20 + 1
```

该测试只在显式 `FURNITURE_WORKFLOW_LOCAL_E2E=1` 且 Job profile 明确为 `LOCAL_E2E` 时启用，普通生产任务不能隐式切换到 Fake Provider。

## 10. 发布前检查结果

| 检查 | 结果 |
| --- | --- |
| Python 全量（API、workflow_core、workflow-engine） | PASS：`113 passed` |
| 确定性 Website E2E | PASS：`1 passed` |
| Python compileall | PASS |
| Web TypeScript | PASS |
| Web ESLint | PASS：0 errors；保留 1 个既有 `no-img-element` warning |
| Next.js production build | PASS |
| `npm audit --omit=dev --audit-level=high` | PASS：`0 vulnerabilities` |
| `scripts/check_public_tree.ps1` | PASS：126 个文本文件 |
| 公开树敏感信息审查 | PASS：只保留空配置变量名、配置模板和显式测试 placeholder，不含真实凭据 |
| `git diff --check` | PASS；仅有 Windows 换行格式提示，无 whitespace error |

## 11. 安全与公司环境边界

- 不复制或读取用户日常浏览器 Cookie、密码、历史或 Profile；L2 使用 Website 自己的隔离持久会话。
- 不自动破解 CAPTCHA、人机验证、WAF、robots、登录或访问控制。
- `HUMAN_REQUIRED`、`ROBOTS_DENIED`、`BROWSER_REQUIRED`、`TARGET_SHORTAGE` 和 `MODEL_DIMENSION_CONFLICT` 均是可审计状态，不通过改成成功来“完成”测试。
- 真实 Brain、独立 Vision、Lux3D 和 Blender 必须由公司电脑的私有环境注入；报告、公开仓库和 Website diagnostics 不显示 key、私有 URL 或本机路径。
- Provider OFF 只表示尚未允许真实建模，不等于生产故障；Ready Pool 也不等于已经有 GLB。

## 12. 公司电脑下一步验收顺序

1. 复制配置模板到私有本地环境并填入正式 Brain/Lux3D/Blender 配置，不把值提交到 GitHub。
2. 打开 Website `/system`，确认 Brain、L2 Browser、Lux3D、Blender 和 Worker 状态与真实环境一致。
3. 用一个无访问挑战的站点完成 Exact-1 live smoke，确认 Source → Image → Vision → Dimension → Gate。
4. 选择 Provider 并设置成本上限，只对 1 个真实产品开启 Lux3D；检查 raw GLB、Blender final bbox、尺寸容差和 ZIP 下载。
5. 再逐步扩大到 Exact-3、5 并发和多站点队列，确认限流、幂等 ledger、恢复和费用边界。
6. Room & Board 单独按同一可见浏览器会话做小批量恢复测试；遇到挑战只人工处理并从 checkpoint 继续。

## 13. 发布记录

- 目标仓库：<https://github.com/eksn425-del/furniture-workflow-website>
- 目标分支：`main`
- 本轮提交内容：Website 源码、回归测试、运行诊断、Blender 尺寸 QA、访问状态分类、同源配置和本报告。
- 未包含：Skills 本体、真实 API key/token、Cookie、个人信息、公司内部材料、数据库、下载图片、GLB、浏览器 Profile 和构建缓存。

最终交付状态：

```text
Website source + local control workflow: COMPANY LARGE-SCALE PILOT READY
Real company provider/model verification: PENDING COMPANY ENVIRONMENT ACTIVATION
```
