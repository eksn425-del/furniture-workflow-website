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

## Alessi：N=1 已实际运行，尺寸取证未完成

- 从网站总览恢复原扫描，而非另造结果。
- 网站 SOURCE 请求已通过 bridge 回答。网站实际调用 browse(home) 和 get_count(decor-and-furniture)，并把工具结果交回下一轮请求。
- 已返回 Homeware 页面导航文字及 683961 字节页面；decor-and-furniture 计数 210，证据为 bounded_product_link_sample，必须保留 ESTIMATED，不写 EXACT。
- 网站执行多轮工具后得到导航与估计数量。修复平面 URL 的父子关系、部门被误过滤、不同子类被合并以及政策链接混入类目等问题；现有旧快照仍含孤立类目，不能宣称完整类目树验收通过。
- UI 创建 `job_0c8010bf848a4928a8ac98a871818c41`，Homeware 下按种子 20260907 的 LCG 选择 bathroom，N=1、Provider OFF。104 为估计值，非精确产品总量。
- 网站发现 Birillo toilet brush、Mr. Cold liquid soap dispenser、Birillo tissue box；Codex 实际查看三张网站下载图片并经 bridge 返回判断。没有新增付费生成。
- 发现历史 source_dimensions 的 width=100、height=18 缺物理单位；修复脚本/CSS 文本污染、拒绝无单位轴数据，并防止旧错误值继续参与尺寸合并。该数字不应解释为官方尺寸。
- 2026-09-07 晚间多次通过网页恢复同一 Job。网站新增有界尺寸收据（实际 URL、标题、可见文字、匹配结果），保存在同任务 browser_session 的 dimension_receipts。修复后 Website 成功展开 More information，读到材质、型号、产地，但本次可见文字仍无尺寸；保留 LOOKUP_INCOMPLETE，而非 OFFICIAL_ABSENT 或外部阻断。
- 当前 Ready=0、交付=0；此站仍为 OPEN_CODE_DEFECT / DIMENSION_LOOKUP，不计入通过。尺寸回退策略和其他官方证据入口仍待闭环。

## Sawyer：真实网页下载复核

- 保留历史同 Job `job_90bbcd1f29d645b98310ee95009861ff`、Lux3D task 3304300。没有重新提交模型。
- 实际点击网页下载按钮并收到浏览器 download 事件；下载目录中的 ZIP 为 3646766 字节，SHA256 `156b9f2382960acd363bb98f45e964f8742d0cad776fec4e805fb4af8d37539c`，含一件 GLB（4000304 字节）。
- 原官方尺寸 18×4×3 in；宽锚点等比缩放后实测 45.72×11.82×8.58 cm，不声称三轴均在 5% 内。该下载是已有成果恢复验收，不是新增随机 Room & Board 全链路验收。

## West Elm：实际浏览器拒绝访问

- L1 有真实首页导航证据；L2 Website 浏览器返回标题 West Elm: 403 - Restricted Access 及明确拒绝文字，无 CAPTCHA 控件。
- 有效 bridge session `4575dbc6df2dd497e7d58379` 为 RESPONDED；更早同类请求曾超时，不计为已接收。
- 当前 ACCESS_BLOCKED，未执行 N=1，不计 Ready 或下载通过。

## Nathan James：新站网页扫描

- 2026-09-07 20:53 从正式网页输入官方 URL、品牌名并点击预检扫描。
- Job `job_2677fe95156443d9bac686f027083f6c`，scan `scan_a851fd5ef4374df2a1b6e91410d2924e`。当前扫描进行中；尚无类目/N=1/下载通过结论。

## 回归范围

- production_convergence、native_site_analysis、domain_agent_v3 三个测试文件共 76 项通过（修复回归，不算真实站点通过）。后续变更须重跑。
- 本文不代表完整 Web build/lint 或全部仓库测试通过。

## 运行与限制

- 使用现有正式 API、网页、worker、CODEX_DEVELOPMENT_BRIDGE；没有第二套 Agent Engine。
- 2026-09-07 恢复时端口 3000/8000 原进程已停止，现重新启动，沿用 website_real 数据目录与原 bridge。
- 脑请求 180 秒超时后，迟到响应不算已接收。实际成功以 session RESPONDED 和网页阶段推进为证据。
- 本轮未新增 Lux3D 付费请求。未推送 GitHub。公司模型能力未实连验证。45 站尚未测完。
