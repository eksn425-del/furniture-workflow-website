# Furniture Workflow Website｜Brain/Vision + Lux3D + Blender 集成迭代报告

日期：2026-08-31  
范围：本机 Website、Local Agent、独立视觉判断、Lux3D Provider 边界、真实 Blender CLI

## 结论

本轮本地集成链已通过：`Local Agent review → Provider contract → real Blender GLB normalization → final bbox/dimension QA → delivery` 可以完整结束，状态为 `COMPLETED`。

真实 Lux3D 生成本轮没有执行。当前 API 进程和本机已检查的私有配置位置都没有 `LUX3D_API_KEY` / `LUX3D_BASE_URL`，因此 Website 正确保持 `Lux3D=OFF`，没有发起任何可能计费或产生未知状态的创建请求。Lux3D 服务只做了只读连通性检查，返回 HTTP 200；这不等于真实建模成功。

因此本轮结果是：

- 本地 Brain/Vision + 真实 Blender：`PASS`
- Website 生产状态、收据、交付链：`PASS`（使用隔离的本地 Provider contract stub，不冒充 Lux3D）
- 真实 Lux3D API 建模：`BLOCKED / NOT RUN`（缺少私有配置）
- 当前可部署结论：配置公司电脑的私有 Lux3D 参数后，可继续做单产品真实付费微测；不能把本轮合成 Provider 结果当成 Lux3D 结果。

## 运行时核对

重启后的 Website `/system` 页面显示：

| 能力 | 实际状态 | 说明 |
| --- | --- | --- |
| Website Brain | `LOCAL_AGENT` | `LOCAL_REVIEW_MODE=agent` 生效，不发外部 Brain API |
| Local Agent Override | `ON` | 本机由 Codex 作为文字/视觉判断者 |
| L2 Browser | `READY` | Chromium、隔离持久化上下文 |
| Database | `READY` | 实际 SQLite 控制平面可用 |
| Blender | `READY` | 本机 Blender 5.0 Alpha CLI 已探测到 |
| Lux3D | `OFF` | `PROVIDER_MODE=disabled`，没有私有凭据 |
| Provider calls | `0` | 没有真实 Provider 创建请求 |

现有 Interior Define Ready Pool 保持原状：已完成的 3 个 Local Agent 候选仍停在 Provider 安全闸门前，没有被本轮错误重跑或伪造交付。

## Local Agent / Vision 观察

本轮查看了一张已保存的 Interior Define 商品图：白底、单一木质桌体、无场景干扰、主体完整可见，颜色为暖棕/胡桃木倾向，材质为木材，属于单品家具，适合 Image-to-3D。该观察与来源名称和结构化尺寸证据可以组成一致的模型输入。

同时，自动化回归使用合成商品证据验证 Website 的 Local Agent receipt 路径，receipt 状态为 `LOCAL_AGENT_REVIEW`。这两件事需要区分：图片观察证明本机多模态判断可用；合成 fixture 只证明代码链路，不替代真实商品的正式 Lux3D 生产结果。

## 真实 Blender 验证

使用真实 Blender CLI 对 GLB 执行：

1. 导入 raw GLB；
2. 读取 bbox；
3. 以目标尺寸规划统一缩放；
4. 在 Blender 中应用 transform；
5. 导出 normalized GLB；
6. 重新读取最终 bbox 并做尺寸容差 QA。

目标尺寸为 `24 × 26 × 31 in`（width × depth × height）。最终结果：

- adapter：`BLENDER_CLI`
- uniform scale：约 `2.0`
- final bbox：约 `0.609600 × 0.660400 × 0.787400 m`
- 轴向尺寸误差：约 `4 × 10^-8` 量级
- dimension QA：`PASS`
- record state：`COMPLETED`

期间发现初次手工测试输入把 glTF 的轴向语义传错了。代码的 canonical 映射是 `width=x / height=y / depth=z`；修正测试目标映射后真实 Blender 结果通过。非统一形变仍会进入 `MODEL_DIMENSION_CONFLICT`，没有为了通过测试放宽安全规则。

## Website 生产管线契约验证

隔离单产品试验使用本地 Provider contract stub 生成可读 GLB，仅用于验证 Website 与 Blender 的接口衔接；没有访问 Lux3D。

- Brain receipt：`LOCAL_AGENT_REVIEW`
- Provider create：`1`
- Provider poll：`1`
- Provider download：`1`
- 事件序列：`DISCOVERY_COMPLETED → ARTIFACT_READY → DELIVERY_COMPLETED → JOB_COMPLETED`
- Blender QA：`PASS`
- 最终状态：`COMPLETED`
- 生成的 GLB：仅为合成回归资产，不作为真实建模交付物

## 本轮最小修复

启动器原来只从 `.env.local` 读取输出目录和 Web 地址，导致公司电脑即使在本地配置文件中填写了 Brain、Lux3D 或 Blender 参数，双击启动时也不会传给 API。

已修复：

- 增加显式本地环境变量白名单；
- 进程环境变量优先于 `.env.local`；
- `.env.local` 只作为被 Git 忽略的本地 fallback；
- 私有 key、密码、token 只传给子进程，启动器不打印；
- 任意未列入白名单的 shell/test 变量不会被转发；
- 增加启动器配置读取回归测试。

这不会改变 Provider 安全策略：没有完整配置、资格确认、幂等记录、成本上限和生产闸门时，Lux3D 仍然不会提交。

## 测试结果

- 全量 Python tests：`114 passed`
- `test_launcher_env.py` + `test_runtime_and_blender.py`：通过
- `test_production_convergence.py`：通过
- Web TypeScript typecheck：通过
- Web ESLint：通过，保留 1 条既有 Next `<img>` 性能 warning，无 error
- 真实 Blender CLI 集成 smoke：通过
- Lux3D 只读连通性：HTTP 200
- Lux3D 实际创建：未执行，避免无凭据请求和未知/计费状态

## 继续真实 Lux3D 微测的条件

在公司电脑或本机的被 Git 忽略 `.env.local` / 进程环境中私下配置：

- `LUX3D_API_KEY`
- `LUX3D_BASE_URL`
- `LUX3D_VERSION`、面数和轮询参数（如需覆盖默认值）
- `BLENDER_WORKER_ENABLED=true`
- `BLENDER_EXECUTABLE` 指向本机 Blender

然后重启 Website，在 `/system` 确认 Lux3D 为可配置、Blender 为 `READY`，只选择 1 个已完成 Local Agent 复核的产品并审批明确成本上限，再执行一次真实创建。任何 create 超时或未知响应都必须保留 checkpoint 并进入 quarantine，不得盲目重试。

本报告不包含任何 API key、私有 URL、浏览器会话、个人路径或生成模型文件。
