# Furniture Workflow Website｜公司试点前 Production Hardening 报告

日期：2026-09-02  
起始提交：`80be42177a240b1de217155108a67c806fc514c6`  
工作分支：`fix/company-pilot-hardening`

## 结论

本轮按小范围 Production Hardening 执行，没有重写 V2 的 Scope、Snapshot、Pagination、Quota、Dimension、Blender 或 Delivery 架构。完成三项公司试点前安全闭环：Brain/Vision fail-closed、Browser 异常语义分离、Provider 硬调用次数上限。

本机没有调用真实公司 Brain、Vision 或 Lux3D，不把离线测试冒充正式服务验收。真实公司环境仍需按 `1 → 3 → 10 → 20/21 → 50 → 100` 逐级放量。

## Brain / Vision fail-closed

- 无远程凭据时默认 `LOCAL_AGENT`，必须提交与当前主图 SHA-256 完全匹配的显式复核。
- `MULTIMODAL_SINGLE_MODEL` 的商品审核必须同时有图片 URL 和媒体 SHA-256；请求体实际携带 `image_url`，receipt 记录视觉输入与审核 hash。
- `TEXT_BRAIN_PLUS_VISION` 使用独立 `WEBSITE_VISION_*` 配置。缺少独立 Vision Provider 时返回 `VISION_PROVIDER_NOT_CONFIGURED`，文本 Brain 不能标记视觉审核完成。
- 独立 Vision 先产生图片 receipt，文本 Brain 只消费该结构化视觉判断；最终四项视觉布尔门使用两端保守合并。
- 远程 receipt 缺失视觉输入、缺失 hash 或 hash 不匹配时保持阻断。
- System Diagnostic 显示 Effective Mode、Effective Mode Source、Vision Input、Local Agent Override，不输出 key、私有 endpoint 或本机路径。

## Browser 异常分类

浏览器路径已拆分为互斥异常：

- `BrowserHumanRequired`：必须同时存在可见验证文字和可见验证控件。
- `BrowserTemporaryFailure`：临时技术页面或 Playwright 导航失败；同一 session 有界重试后返回 `TEMPORARY_FAILURE`。
- `BrowserAccessDenied`：静态拒绝页但没有验证控件，返回 `ACCESS_CHANGE_REQUIRED`。
- `BrowserRuntimeMissing`：Playwright/系统浏览器运行时缺失。
- `ROBOTS_DENIED` 保持策略阻断，不升级 Browser。

临时故障和静态拒绝不再伪装成需要用户点击的人机验证。

## Provider 硬调用上限

- 审批 receipt 和 Job plan 新增 `approved_provider_call_limit`。
- Exact-N 未显式填写时安全默认 N；ALL 任务启用付费 Provider 时必须显式给出正整数上限。
- 每次 `create_task` 前重新读取持久 Provider ledger。
- 已确认 task、`CREATE_IN_FLIGHT`、`SUBMISSION_UNKNOWN` 都占用额度；`SUBMISSION_UNKNOWN` 不会自动释放或重提。
- 达到上限时返回 `PROVIDER_CALL_LIMIT_REACHED`，不会发送第 N+1 个 POST。
- UI 将成本上限和调用次数上限显示为两道独立硬安全门。
- Provider OFF 不受付费审批影响，仍只形成 Ready Pool。

## 最小跨站 smoke

| 项目 | 结果 |
|---|---|
| Interior Define 入口/类目 | `READY`；36 个类目，36 个 EXACT count |
| Nathan James 入口/类目 | 入口 `READY`；taxonomy `PARTIAL`；49 个类目，其中 20 个 EXACT count |
| Exact-1 / Exact-3 Provider OFF | 确定性回归通过，形成对应 Ready Pool |
| Generic pagination > 1 page | 确定性回归通过，cursor 访问两页后才标记 exhausted |
| Deterministic full E2E | 21 个候选 → 21 个 Provider ledger / Blender QA → 20 + 1 两批交付 |

外部站点的 `PARTIAL` 是证据状态，不是伪造成功，也不阻断本轮安全加固。

## 测试与公开安全门

完成项：

- Python API / workflow-engine 全量测试：`115 passed`
- Brain/Vision、Browser、Provider safety 定向回归
- convergence、product acquisition、Blender 和 deterministic 21 → 20 + 1 E2E
- frontend typecheck、lint、production build
- `npm audit --audit-level=high`：0 vulnerabilities
- `git diff --check`
- public tree / sensitive scan

可见 Website UI smoke 也已完成：System 页面正确显示 `LOCAL_AGENT`、Effective Mode Source 与 `LOCAL_AGENT_EXPLICIT_REVIEW`；Exact-3 Job 的审批表默认显示成本上限和 `Provider 调用上限 = 3`，并明确提示 `SUBMISSION_UNKNOWN` 占用额度。测试没有确认最终付费审批，也没有产生外部 Provider POST。

公开树检查共扫描 136 个受控源文件；额外敏感模式检查未发现真实 key、私钥、本机用户路径、数据库、GLB、ZIP、浏览器 profile/cookie 或 Skills。

前端 lint 保留一条既有的 `@next/next/no-img-element` 性能警告；它用于外部商品主图预览，不是安全或功能错误。

## 公司验证边界

公司电脑首次启用建议使用 `MULTIMODAL_SINGLE_MODEL`，前提是所接 endpoint 明确支持图片输入。只有真实配置独立 `WEBSITE_VISION_*` 并生成独立 receipt 时，才可选择 `TEXT_BRAIN_PLUS_VISION`。

首次真实 Provider 测试必须从 1 个模型、并发 1 开始。出现 `SUBMISSION_UNKNOWN`、hash mismatch、`VISION_PROVIDER_NOT_CONFIGURED`、`MODEL_DIMENSION_CONFLICT` 或 `PROVIDER_CALL_LIMIT_REACHED` 时停止放量并保留同一 Job/ledger，不删除账本或自动重提。
