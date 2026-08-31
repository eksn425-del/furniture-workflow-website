# 先看我：Furniture Workflow Website

这是一个只包含 Website 源码的本地项目。它把公开网站采集与生产控制拆成可审计的阶段：

1. 输入公开 URL，预检并识别类目；
2. 展示有证据的数量，允许选择一级或二级范围；
3. 按 Exact N / Up To N 形成任务策略；
4. 发现候选产品并做商品身份、图片绑定、视觉与尺寸审查；
5. 通过 Production Gate 后才允许进入可选的 3D Provider；
6. 写入可恢复的状态、事件、收据与交付索引。

## 快速启动

```powershell
Copy-Item .env.example .env.local
python -m pip install -r services/api/requirements.txt
python -m playwright install chromium
Set-Location apps/web
npm ci
npm run build
Set-Location ../..
python launch_website.py
```

默认只启动本地控制面，Provider 为 OFF，不需要任何外部 AI 密钥。将来的 Brain 可以选择单一多模态模型，或文本 Brain 加独立 Vision 模型；配置只从 `WEBSITE_BRAIN_*` 读取。Lux3D 只通过 `LUX3D_*` 配置，且必须经过明确成本审批和安全收据。

公开站点的 L2 浏览器默认使用隔离的 Playwright Chromium。也可以设置 `WEBSITE_L2_BROWSER_ENGINE=msedge` 或 `chrome` 使用已安装的系统浏览器，但 Website 仍使用独立 profile，不占用个人浏览器的登录态。

## 不要提交的内容

不要提交 `.env.local`、数据库、浏览器 profile/cookies、抓取媒体、运行日志、Candidate Pool、GLB、个人信息、公司内部文档或任何真实 Provider 凭据。`.gitignore` 已覆盖常见运行产物；提交前仍应执行一次敏感信息扫描。

## 修改边界

保持 `workflow-event.v2`、持久化状态、Source Policy、身份绑定、生产安全闸门和恢复语义兼容。采集失败或访问挑战必须显式保留为 `UNKNOWN`、`BLOCKED` 或人工处理状态，不能用猜测值伪造成功。
