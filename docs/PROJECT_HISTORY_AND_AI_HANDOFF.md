# Furniture Workflow Website

## Project history, test record, and AI handoff

更新时间：2026-08-29  
适用范围：Website 源码仓库，不包含 Skills、公司内部材料、真实凭据或生产数据。

## 0. 这份文档如何使用

这份文档是 Website 源码的上下文补充，不替代源码本身。

建议新的 Website AI 按以下顺序读取：

1. 先读本文件，了解目标、历史问题、测试事实和安全边界。
2. 再读取 GitHub 仓库中的 `README.md`、`docs/ARCHITECTURE.md`、`docs/LOCAL_DEVELOPMENT.md` 和 `docs/SECURITY.md`。
3. 最后检查 `apps/web`、`services/api`、`packages/workflow_core`、`packages/workflow-engine`、`workers` 和 `tests`，把本文件中的问题映射到实际代码和测试。

Website 源码仓库：

<https://github.com/eksn425-del/furniture-workflow-website>

当前公开版本的本地/远端 Git 提交为：

`d0634efc3a7efec9a633cd0318a1c28a78586fdf`

## 1. 项目目标

Furniture Workflow Website 是一个 local-first 的家具产品发现、审核、建模和交付控制台。目标不是做一个只展示页面的 Demo，而是把下列链路连接成可恢复、可审计的生产工作流：

```text
公共网站 URL
→ 访问预检
→ 类目识别与数量证据
→ 用户选择范围
→ 候选商品发现
→ 产品身份 / 图片 / 视觉审核
→ 生产资格 Gate
→ Ready Pool
→ 可选的 3D Provider
→ GLB 校验与交付
```

后续正式环境可以接入两种大脑模式：

- 单一多模态模型；
- 文本 Brain 加独立 Vision 模型。

本公开 Website 仓库只保留接口、状态机、校验、适配器和测试，不包含任何真实服务凭据。没有私有配置时，系统应明确显示未配置或未执行，不能伪造成功。

## 2. 当前仓库边界

### 已包含

- Next.js Website 前端和操作控制台；
- FastAPI 控制面与持久化数据模型；
- 类目、数量、任务、候选池、产品 Registry 和交付状态；
- Source Policy、产品身份、图片证据、生产 Gate 和命名治理；
- L1-first / L2-on-demand 的工作流接口；
- 本地人工审核和 HUMAN_REQUIRED 恢复路径；
- 可选 Brain、Vision、Lux3D 类 Provider 的安全边界；
- Python、前端类型检查、Lint、构建和公开树扫描测试；
- 通用演示 fixture 和本地开发文档。

### 明确不包含

- Skills 包及其测试数据；
- 任何 API Key、Token、密码、Cookie 或登录会话；
- 公司内部文档、个人信息、内部网络地址和本机路径；
- 真实数据库、历史生产记录、浏览器 Profile、下载图片和 GLB；
- `node_modules`、Next 构建目录和其他运行时缓存。

这里的“去除 API”指去除真实凭据和私有服务地址，同时保留必要的 API 路由和 Provider 接口，使其他 AI 能理解并继续开发完整 Website 架构。

## 3. 之前完成的主要迭代

### 3.1 从页面 Demo 收口到可恢复工作流

早期主要问题是“页面看起来可以操作，但扫描结果、任务、候选商品、建模和交付状态之间不完全连通”。后续迭代完成了：

- 网站和扫描任务持久化；
- 类目选择真正驱动候选采集范围；
- Exact N、Up To N、All 三种数量策略；
- Candidate Pool、永久 Registry 和 Checkpoint/Resume；
- Source → Image → Vision → Gate 的证据链；
- HUMAN_REQUIRED、TARGET_SHORTAGE、READY_FOR_MODELING 等明确状态；
- Provider Ledger、幂等键、重复提交保护和不确定提交恢复；
- 真实交付收据成立后才显示下载结果。

### 3.2 数量状态治理

历史测试暴露出一个高频误导：扫描失败或尚未统计时，页面容易把数量显示成 `0` 或 `UNKNOWN`，用户无法区分“真的没有商品”和“系统还不知道”。

当前正确语义是：

- `0`：已经取得可信证据，确认数量为零；
- `UNKNOWN`：尚未取得可靠数量证据；
- `NEEDS_HUMAN`：自动扫描被访问、验证或页面状态阻断，需要人工接管；
- `TARGET_SHORTAGE`：范围已耗尽但没有达到用户的 Exact N 目标；
- `UP_TO_N_ACCEPTED`：在 Up To N 模式下，当前范围已合理耗尽，可以少于上限；
- `READY_FOR_MODELING`：完成资格审核并可进入建模，不代表已经生成 GLB。

任何失败都不能通过把 `UNKNOWN` 改成 `0` 来掩盖，也不能为了凑数量放宽质量 Gate。

## 4. 历史测试记录

以下记录只总结项目行为、问题和结论，不包含任何凭据或私有运行数据。

### 4.1 Room & Board → Bath → Vanities

测试目标是根据历史快照约 13/14 个 Vanity 商品，实时重新发现当前有效商品，并验证：

- Product Identity；
- Main Product Binding；
- Scene7 layered image；
- Source ↔ Image ↔ Vision 一致性；
- L1-first / L2-on-demand；
- HUMAN 验证后的恢复；
- 状态同步和不超过 50 字符的名称。

已观察到的事实：

- 使用用户可见浏览器路径时，页面和商品类目曾经可以正常打开，说明“网站永远无法访问”并不成立；
- 同一站点在不同浏览器会话、不同时间和不同访问频率下会返回正常页面，也会返回网站的 “Technical difficulties / Please try again later” 页面；
- 有时没有看到明显 CAPTCHA 按钮，仍然会被站点边缘访问策略阻断；这应记录为访问阻断或 HUMAN_REQUIRED，不能误判成解析器没有商品；
- 浏览器被用户接管或人工验证后，任务需要从同一个 checkpoint 继续，而不是重新创建一套商品身份；
- 官方商品名可能相同或过长。来源名称优先保留，唯一性只用于内部记录和文件交付，不应篡改来源身份；最终对外名称必须严格不超过 50 个字符（含空格）。

当前 Website 侧应遵循的结论：

- 先保存访问失败证据，再尝试有限、可记录的恢复；
- 不绕过 CAPTCHA、WAF、登录或访问控制；
- 浏览器状态和爬虫状态必须同步到同一个 Job；
- 采集未完成时显示不完整/人工状态，不显示伪造的成功数量；
- Provider 关闭时先形成 Ready Pool，不能把“尚未调用 Lux3D”当作失败。

### 4.2 Anthropologie → Kitchen & Dining

这是一次多类别零售站点资格测试，要求系统自行从当前商品池发现、筛选、Variant 去重、判断 Scope、视觉审核，并得到 Exact-10 Modeling-Eligible Products。

重点验证项：

- `MULTI_CATEGORY_RETAILER` 入口识别；
- Style No. / Color Code 等商品身份信号；
- Variant 去重；
- Set / Bundle 不误拆成多个独立商品；
- 第三方品牌不误判为站点自有品牌；
- L1 轻量扫描，只有需要时才进入 L2；
- 图片、商品身份、视觉判断和最终命名的一致性；
- 空格在内严格不超过 50 字符。

测试得到的工程结论：

- 类目页、商品卡、商品详情和变体信息不能只依赖一种 DOM 结构；
- 对多品牌、多变体零售站点，身份键应优先使用站点商品编号、Style No.、Variant/Color Code 和规范化详情 URL 的组合；
- Set/Bundle、配件和场景图必须进入 Scope 判断，不能仅凭标题关键词放行；
- 本机 Local Agent / Multimodal Reviewer 可以完成离线视觉判断，但真实 Provider 建模结果必须单独标记；
- Exact-10 是资格目标，不应通过放宽视觉质量或身份证据凑数。

### 4.3 Interior Define 类目扫描

在 Website 交互测试中，曾观察到类目识别页面出现类似以下状态：

- 一级类目数量可以显示；
- 子类目数量可以显示；
- 已选择数量为 0 是合理的初始状态；
- 商品可见/估算合计显示 `UNKNOWN`，说明数量证据尚未完成，而不是商品数量为零。

随后还出现过“网站库中的一级类目、二级类目都显示为 0”的情况，同时页面提示最新扫描需要人工或可以恢复同一扫描。这暴露出两个问题：

1. 扫描失败状态不能覆盖最近一次可信快照；
2. UI 必须区分“未扫描”“扫描失败”“人工待处理”“真实零结果”。

Website 现在的修复方向是保留 scan evidence、错误原因和恢复入口，禁止失败回写为可信的空类目。

### 4.4 Francfranc 类目采集

Francfranc 用于检验通用零售站点的类目、价格筛选、分页和商品详情适配能力。它不是 Room & Board 的专用规则，重点是：

- 站点适配器不能硬编码单一品牌 DOM；
- 价格、排序、分页参数必须被记录为 Scope；
- 商品列表发现和详情证据要能追溯到同一商品身份；
- 站点没有真实 Provider 配置时，只完成发现、审核和 Ready Pool，不伪造建模交付。

### 4.5 浏览器路径对比

历史操作中比较过两种方式：

- Website/内置浏览器路径：适合后台任务、可复现状态和持久化 checkpoint，但通常不共享用户日常浏览器的登录、Cookie、扩展和人工验证状态；
- 用户可见的外部浏览器路径：更容易复用用户已经打开的页面和人工验证结果，但必须记录标签页、会话和接管状态。

工程上不能假设二者拥有相同的浏览器数据。遇到需要人工验证的站点，应明确显示 HUMAN_REQUIRED 并等待用户接管；不能偷偷读取 Cookie、密码或浏览历史，也不能自动绕过验证。

## 5. 历史问题、原因与当前处理

| 问题 | 主要原因 | 当前处理原则 |
| --- | --- | --- |
| 类目数量显示未知或变成 0 | 扫描证据未完成、失败状态覆盖快照、UI 状态语义不清 | `UNKNOWN` 不等于 0；保留最近可信快照和失败证据 |
| Room & Board 偶发 Technical difficulties | 站点边缘访问策略、会话/频率/时间差异，不一定是解析器错误 | 保存证据、有限恢复、必要时 HUMAN_REQUIRED，不绕过访问控制 |
| 看不到 CAPTCHA 但任务停止 | 阻断可能发生在 CAPTCHA 之外或页面内容被替换 | 识别页面级访问阻断，进入可恢复人工状态 |
| 内置浏览器和用户浏览器行为不同 | 浏览器会话、扩展和登录状态隔离 | 使用明确的浏览器通道和可见接管，不假设共享 Cookie |
| 浏览器慢、鼠标不动或自动关闭 | 多个自动化阶段抢占同一会话、缺少稳定 checkpoint、页面等待过长 | 单任务单会话、持久 checkpoint、状态可恢复；不要依靠无限等待 |
| 官方名称重复或过长 | 官方商品名相同、变体名相近、来源名超过交付限制 | 来源名称优先，内部 ID 区分；最终名称含空格不超过 50 字符 |
| 图片绑定错误 | 列表图、详情图、Scene7 layered 图和变体图未统一到同一身份 | Main Product Binding + Source/Image/Vision lineage，证据不足时不放行 |
| 把 Ready Pool 当成已建模 | Provider 关闭或真实建模未执行 | Ready Pool 只能表示资格通过；GLB 必须有真实交付收据 |
| 为通过测试而放宽 Gate | 目标数量压力导致质量规则被绕过 | Exact N 不足就显示 TARGET_SHORTAGE，不能降低安全规则 |

## 6. 当前已验证结果

公开 Website 副本已经完成：

- Python 回归测试全部通过；
- 前端 TypeScript 检查通过；
- ESLint 通过；
- Next.js 生产构建通过；
- 公开树凭据/路径/运行产物扫描通过；
- 全量 npm audit 和生产依赖审计均为 0 漏洞；
- 本地 Git 与 GitHub `main` 的 commit SHA 已对账一致。

这些是源码和离线/确定性测试结果，不代表已经在本机调用真实 Brain、Vision 或 Lux3D 付费服务。

## 7. 给后续 Website AI 的工作规则

### 7.1 开始任何修改前

- 先读本文件和仓库 README/架构/安全文档；
- 检查现有测试和状态模型；
- 先复现问题并保存失败证据；
- 优先做最小修复，不重写已验证的跨站点能力；
- 保持 Website 与 Source Policy、Production Gate 的语义一致。

### 7.2 采集与浏览器

- 优先使用合法、公开、可审计的站点入口；
- 不绕过 CAPTCHA、WAF、登录、robots 或访问控制；
- 遇到人工验证，暂停在 HUMAN_REQUIRED 并保留恢复点；
- 不读取或上传用户 Cookie、密码、浏览历史、浏览器 Profile；
- 所有重试必须有上限、原因和状态记录；
- 分类扫描失败时不能写入虚假的 0。

### 7.3 产品与图片

- 先确定 Product Identity，再绑定主商品图片；
- 先 L1 获取商品卡、URL、身份、基础图片和类目信息；
- 只有在 L1 不足以做决定时才进入 L2；
- Scene7 layered、变体图片、场景图和详情图必须分别记录角色；
- Source、Image、Vision 和最终 Gate 必须能追溯到同一商品记录；
- Set/Bundle、配件、第三方品牌和变体不能仅凭标题放行。

### 7.4 命名与交付

- 来源名称优先；
- 内部去重使用稳定身份键，不要用改名掩盖身份冲突；
- 最终品牌名和非品牌名，包含空格，严格不超过 50 个字符；
- `READY_FOR_MODELING` 不等于 GLB 已生成；
- 只有真实 GLB 校验、哈希、Registry 和交付收据成立后才能显示下载。

## 8. 下一轮建议验收顺序

1. 使用一个没有访问验证的公开零售站点，验证类目识别和可信数量显示。
2. 选择一个类目创建任务，确认所选 Scope 真正限制候选池。
3. 验证列表卡、详情页、变体和图片绑定到同一 Product Identity。
4. 在 Provider 关闭时完成候选审核并看到 Ready Pool。
5. 模拟或接入私有 Provider 后，从同一 Job 恢复，确认不重复提交。
6. 制造一次扫描失败或人工验证状态，确认页面不显示虚假 0，并能恢复。
7. 运行命名边界测试，确认所有最终名称不超过 50 个字符。
8. 最后再进行真实 Provider 和真实站点测试；真实结果必须与离线结果分开记录。

## 9. 一句话交接结论

请把本 MD 当作“为什么这样设计、之前哪里出过问题、什么结果可信”的项目记忆，把 GitHub 仓库当作“现在具体代码和测试如何实现”的事实来源；两者合起来，才能完整理解并继续迭代 Furniture Workflow Website。
