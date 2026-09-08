# 公司电脑部署与测试说明

更新日期：2026-09-08。使用原仓库 main，保留原产品和历史。当前是开发验收版：45站中尝试7站，38站未测；集中修复的离线回归通过，不代表全部实站已通过。最新集中反馈见 `WEBSITE_CONSOLIDATED_FIX_REPORT_20260908.md`；该报告的“未推送”描述是当时的历史状态。

## 首次安装（Windows PowerShell）

先安装 Git、Python 3.11+、Node.js 20.9+。在希望存放项目的目录执行：

```powershell
git clone https://github.com/eksn425-del/furniture-workflow-website.git
Set-Location furniture-workflow-website
git switch main
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r services/api/requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env.local
Set-Location apps/web
npm ci
npm run build
Set-Location ../..
.\.venv\Scripts\python.exe launch_website.py
```

入口：http://127.0.0.1:3000。保持启动器窗口打开，关闭即停止服务。默认 Provider OFF，没有配置大脑时不能自动完成视觉判断或建模。仓库不包含本机历史任务、数据库、图片或模型，首次启动没有这些历史业务是正常的。

## 私有配置

只在公司电脑的 `.env.local` 填写真实值，禁止提交到 GitHub、报告或聊天。系统环境变量优先于该文件。输出目录应使用本机有写权限的专用目录，不要照抄旧报告中的另一台电脑路径。

- 公司多模态接口：`WEBSITE_MODEL_MODE=MULTIMODAL_SINGLE_MODEL`，设置 `WEBSITE_BRAIN_BASE_URL`、`WEBSITE_BRAIN_API_KEY`、`WEBSITE_BRAIN_MODEL`。需验证兼容接口接受真实图像 data URL；当前公司 API 尚未实连验收。
- 开发桥接：`WEBSITE_MODEL_MODE=CODEX_DEVELOPMENT_BRIDGE`，设置本机 `CODEX_BRIDGE_ROOT`。仅创建文件夹不等于大脑在线，必须有活动开发会话处理网站请求；仓库不会自动启动一个常驻 Codex 大脑。
- Blender：安装真实 Blender 后设置 `BLENDER_WORKER_ENABLED=true` 和 `BLENDER_EXECUTABLE` 的本机绝对路径。仓库不附带 Blender。
- Lux3D：通过安全渠道取得已有凭据，配置 `LUX3D_*`。首次只做 Provider OFF 检查；开启收费建模前必须单独核对费用授权、次数限制和已有任务账本。unknown 状态不得重发。
- 仅查看模式：设置 `WEBSITE_BACKGROUND_WORK_PAUSED=true` 后重启，停止后台自动调度；仍不要点击开始生产。正式继续测试前改为 false 并重启。

本地启动不等于安全的公网部署。不要直接开放端口到公网；公司多人访问前需配置认证和受控网络，并另行验收。

## 后续更新

先停止启动器，在项目根目录运行 `git status`。若有本地修改先保留，不要 reset；确认可更新后运行 `git pull --ff-only origin main`。依赖更新后重新执行 pip install、npm ci、npm run build，再启动。`.env.local` 和运行数据不随源码更新迁移或覆盖。

检查 `/system` 的 API、数据库、浏览器、Blender 和大脑状态。不要把目录已配置当作大脑在线；先做一个 Provider OFF 的小样本，检查图片、类目和尺寸证据，遇到阻断记录真实状态。
