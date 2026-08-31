# Furniture Workflow Website｜公司电脑真实测试启动 Prompt

你现在接手的是 Furniture Workflow Website 公共源码。请先完整阅读仓库内的 `README.md`、`AGENTS.md`、`docs/ARCHITECTURE.md`、`docs/SECURITY.md`、`docs/LOCAL_DEVELOPMENT.md` 和最近的迭代报告，再开始操作。

你的角色是：开发者、QA、Website UI 操作员、Local Agent Brain、视觉审核员和故障诊断员。目标是验证 Website 从“导入网站 → 识别类目/数量 → 选择类目 → 发现商品 → Source/Image/Vision 一致性 → 候选池 → Provider 建模 → Blender QA → Delivery 下载”的完整链路。

## 重要安全边界

- 这是脱敏公共源码；仓库和压缩包不包含真实 API key、公司隐私、浏览器会话、数据库、日志、模型产物或个人路径。
- 任何私有 key、密码、token、公司内网 URL 只写在本机被 Git 忽略的 `.env.local` 或进程环境中，不要贴到聊天、Prompt、截图或提交记录。
- 默认 Provider 为 OFF。只有单产品、明确成本上限、资格检查、幂等记录和生产闸门全部通过后，才允许真实 Lux3D 请求。
- 本轮先做 1 个真实产品微测，不要直接批量建模或并发调用。
- 第三方站出现 CAPTCHA、HUMAN、登录、WAF、robots 或访问限制时，不要破解；保存 checkpoint，记录准确 blocker，继续其他可测试内容。
- 不要把 `UNKNOWN` 数量改成 0，不要为了通过测试放宽 Source Policy、视觉审核、尺寸冲突或生产安全规则。

## 第一步：本机配置

1. 复制模板：

   ```powershell
   Copy-Item .env.example .env.local
   ```

2. 在本机私下填写需要的配置。不要把值返回给聊天：

   ```text
   LOCAL_REVIEW_MODE=agent
   WEBSITE_MODEL_MODE=LOCAL_AGENT

   # 公司已有 Brain API 时才填写；本轮也可以让 Local Agent 工作
   WEBSITE_BRAIN_API_KEY=
   WEBSITE_BRAIN_BASE_URL=
   WEBSITE_BRAIN_MODEL=

   # 真实 Lux3D 微测时由公司配置填写，不要使用占位值
   LUX3D_API_KEY=
   LUX3D_BASE_URL=
   LUX3D_VERSION=v3.0-standard
   LUX3D_FACE_COUNT=60000

   # 指向本机真实 Blender
   BLENDER_WORKER_ENABLED=true
   BLENDER_EXECUTABLE=
   ```

3. `launch_website.py` 只读取显式白名单；进程环境变量优先，私有配置不会打印。若公司安全策略不允许 `.env.local`，改用同名进程环境变量。

## 第二步：启动与诊断

在仓库根目录运行：

```powershell
python launch_website.py
```

打开 `http://127.0.0.1:3000/system`，核对实际状态：

- API：`READY`
- Database：`READY`
- Brain：公司模式或 `LOCAL_AGENT`
- L2 Browser：`READY`
- Blender：`READY`
- Lux3D：只有私有 key 和 endpoint 都正确时才应显示 `READY`
- 不显示 key、私有 URL、会话目录或本机隐私路径

若没有真实 Lux3D 配置，保持 Provider OFF，只运行本地候选池和 deterministic E2E，不要伪造“模型已生成”。

## 第三步：先跑无网络回归

```powershell
$env:PYTHONPATH = "$(Get-Location);$(Get-Location)\services\api;$(Get-Location)\packages\workflow-engine\src"
python -m pytest -q
Set-Location apps/web
npm run typecheck
npm run lint
npm run build
Set-Location ../..
python -m pytest -q services/api/tests/test_website_e2e.py
```

记录完整结果。允许保留已知 lint warning，但不能忽略测试 error、类型错误、构建错误或安全扫描问题。

## 第四步：Website UI 真实操作

使用 Website 页面完成：

1. 新建一个公开网站；
2. 等待扫描完成并查看一级/二级类目；
3. 数量未知时显示 `UNKNOWN`，不要显示 0；
4. 选择一个有明确产品证据的类目；
5. 建立新 Job，使用 `EXACT_N=1`；
6. 先运行 Local Agent 视觉审核，确认单品、主体完整、图片绑定正确、来源证据一致、风格/颜色/材质和尺寸链有 receipt；
7. 官网有尺寸时优先使用官网尺寸；无尺寸时才标记 `AI_ESTIMATED`；
8. 检查名称含空格在内不超过 50 个字符；
9. 只有候选达到 `READY_FOR_MODELING` 后，才进入 Provider 步骤。

## 第五步：真实 Lux3D 单产品微测

只选择 1 个已通过 Local Agent/视觉审核的候选。由页面选择 `Lux3D`，填写本次明确的最高成本上限并审批；审批本身不应立即提交模型请求。确认 Job 的 `PRODUCTION_READY` 后再点击开始生产。

观察并记录：

- Provider create/poll/download 各自次数；
- provider task id 是否持久化；
- 网络超时、失败或未知响应是否进入 quarantine；
- 是否错误重复提交；
- raw GLB 是否保存；
- Blender 是否完成导入、bbox、统一缩放、apply transform、导出和最终 bbox；
- 三轴尺寸误差是否在容差内；
- 若必须明显非统一拉伸，必须进入 `MODEL_DIMENSION_CONFLICT` / review；
- 最终是否生成可下载 Delivery 和 manifest。

真实 Lux3D 成功必须同时有：真实 provider receipt、真实 task 状态、真实 raw GLB、Blender QA receipt、最终 delivery。只有本地 stub、Fake Lux3D 或 Fake Blender 只能算回归测试，不算真实建模成功。

## 第六步：失败恢复与报告

遇到问题时不要删除 Job 或覆盖证据：

- 保存 Job、run、事件、candidate pool 和 provider ledger 的 checkpoint；
- 分类为 `HUMAN_REQUIRED`、`SITE_OFFLINE`、`ROBOTS_DENIED`、`ACCESS_CHANGE_REQUIRED`、`UNSUPPORTED_STRUCTURE`、`PROVIDER_CONFIGURATION_INCOMPLETE`、`MODEL_DIMENSION_CONFLICT` 或准确的其他 reason code；
- 重新打开 Website 或刷新页面，确认状态可恢复且 polling 不串 Job；
- 只做最小修复，新增 regression fixture，再重跑受影响测试；
- 报告 PASS / PARTIAL / FAIL，不能把 blocker 改写成成功。

最终输出至少包括：测试网站和类目、发现/合格/建模/交付数量、每个阶段状态、provider 调用次数、尺寸 QA、下载文件是否可用、失败证据和下一步。

## Definition of Done

本轮单产品达到以下条件才可以报告真实成功：

`READY_FOR_MODELING → PRODUCTION_READY → real Lux3D task completed → raw GLB → real Blender QA PASS → Delivery available`

如果缺少真实 Lux3D 私有配置，诚实报告为“Local Agent + real Blender 回归通过，真实 Lux3D 未执行”，然后停止在安全闸门前。
