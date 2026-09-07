# Website 网页实测续测记录（2026-09-07，持续更新）

本文件是本轮增量证据，不替代 45 站完整矩阵，不表示全部验收通过。

## Interior Define：READY_ONLY

- 原任务：`job_080c8f963eea4729b1604e6d2aae4398`。选中 `/dining/all-dining-tables`，页面类目数量为 22，目标 N=1，Provider OFF。
- 前轮原扫描恢复得到 36 个类目；不把父子类目数累加当成全站唯一商品总数。
- 2026-09-07 通过网页“恢复同一 Job”重启流程，网站发现的三个候选和媒体均复用，没有外部代办采集或手填产品池。
- Codex 逐一实际查看网站保存的 Nina、Matteo 方桌、Matteo 长桌图片，通过正式 bridge 返回视觉判断。三张图均为完整单桌、干净背景。Matteo 的 30 in 高度明确为 AI_ESTIMATED，不是官方值，未用于冒充尺寸补查成功。
- Nina 图片 SHA256：`1fdf296c0090a40b320d66fd18e0e2acf39eed78aa984aabfca13af43e2442d9`；网站请求提供尺寸数值 48×48×30，正式尺寸阶段进入 DIMENSION_READY。
- 复现缺陷：另外两件备用候选尺寸补查 TEMPORARY_PAGE_FAILURE，让已有可推进 Nina 的 N=1 整体暂停。
- 修复：进度分支存在 DIMENSION_READY/NAMING_READY/CATALOG_READY/MODEL_INPUT_LOCKED 候选时，不让备用尺寸阻断抢先结束任务；无进度分支仍保留真实阻断。
- 新增 fixture 回归 `test_dimension_pending_reserve_does_not_preempt_ready_target`；与浏览器挑战保护、扫描回归共 5 项通过。fixture 不计入真实站点通过数。
- 修复后再次点击原任务“恢复同一 Job”，网页确认 Nina Dining Table（17 字符）进入 MODEL_INPUT_LOCKED，显示 1/1 Ready Pool、合格 1、已交付 0、Provider calls 0。
- 最终停在 PROVIDER_SAFETY / PROVIDER_REQUIRED，符合未审批新付费生成的边界。没有本次新模型下载，不记 END_TO_END_PASS。

## Alessi：扫描 Agent 实测进行中

- 从网站总览恢复原扫描，而非另造结果。
- 网站 SOURCE 请求已通过 bridge 回答。网站实际调用 browse(home) 和 get_count(decor-and-furniture)，并把工具结果交回下一轮请求。
- 已返回 Homeware 页面导航文字及 683961 字节页面；decor-and-furniture 计数 210，证据为 bounded_product_link_sample，必须保留 ESTIMATED，不写 EXACT。
- 正在继续由网站执行数量和产品发现工具；暂未宣称 Ready 或端到端通过。

## 运行与限制

- 使用现有正式 API、网页、worker、CODEX_DEVELOPMENT_BRIDGE；没有第二套 Agent Engine。
- 2026-09-07 恢复时端口 3000/8000 原进程已停止，现重新启动，沿用 website_real 数据目录与原 bridge。
- 脑请求 180 秒超时后，迟到响应不算已接收。实际成功以 session RESPONDED 和网页阶段推进为证据。
- 本轮未新增 Lux3D 付费请求。未推送 GitHub。公司模型能力未实连验证。45 站尚未测完。
