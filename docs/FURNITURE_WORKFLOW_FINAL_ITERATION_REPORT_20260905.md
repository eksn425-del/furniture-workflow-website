# Furniture Workflow Website — Final Iteration Report (2026-09-05)

本报告是附件《FurnitureWorkflow_Codex_Final_Iteration.md》的执行记录，不是建议清单。它保留真实失败、访问阻断、超时和未验证状态；任何没有证据的项目都没有被写成 PASS。

## 1. 基线、范围与发布状态

| 项目 | 结果 |
|---|---|
| 本轮基线 | 本地 V3 `53331e22463e9e706e56d72eee8cf121fa772d50`（`53331e2`） |
| 原远端 | `https://github.com/eksn425-del/furniture-workflow-website.git` |
| 开始时远端 `main` | `7d6e098e9623f907df828b05d7a00cbad2ba4821` |
| 远端增量 | 相对 `53331e2` 没有新的有效提交；V3 保留完整 |
| 本轮模式 | 继续使用现有 Website Brain / ProductionPipeline；没有创建第二套 Agent Engine、第二个产品或平行数据库 |
| Provider | `OFF`；没有发起 Lux3D 或其它付费建模调用 |
| GitHub 发布 | **未推送**。G4/G5/G8 尚未全部通过，按附件第 13 节不能把未成熟成果发布到 `main`。 |

本轮修改已提交在本地分支 `codex-v3-iteration-20260904`，可供复核；远端 `main` 没有被覆盖、force push 或改写历史。

## 2. F01–F14 缺陷闭环

| 缺陷 | 处理结果与证据 |
|---|---|
| F01 未初始化 profile | `SiteScanRuntimeService._execute` 显式初始化 `existing_profile_payload`；新增测试通过实际 runtime 覆盖不存在、`null`、空串、数组、坏 JSON、空对象和有效对象。 |
| F02 空 evidence 升 VALIDATED | `finish_site_profile` 和持久化层都要求 taxonomy/product/pagination/PDP/image/dimension 能力证据；空 evidence 保持 `DRAFT`。 |
| F03 旧 STALE 污染新 profile | profile promotion 使用版本/状态检查和当前 validated 保护；新扫描不会把旧 stale 证据拼入新的 validated profile。 |
| F04 空尺寸误判官方缺失 | 尺寸查找保存 `OFFICIAL_LOOKUP_INCOMPLETE`、`OFFICIAL_ABSENT_CONFIRMED`、checked pages、completed 和 official_absent，空返回不再等同于官方明确不存在。 |
| F05 部分尺寸与混合单位 | width/depth/height 按轴保存 value/unit/source/evidence，再统一换算；cm 与 in 的补全不会把原值整体误解释成另一单位。 |
| F06 capacity retry 计费 | `attempts`、`billable_attempts`、`created`、`unknown` 分离；容量拒绝不消耗 billable provider slot；生产回归覆盖。 |
| F07 Generic Core 失败后的真实恢复 | 重复 cursor 现在进入同一 Website Brain + `inspect_pagination` / `discover_products` 工具恢复；模型只能返回证据中的 URL/cursor，pipeline 自己抓取和解析，不能提交模型伪造商品。 |
| F08 browser escalation | `escalate_browser` 使用同一持久 session 目录和 resume token；真实 Interior Define 请求返回 `BROWSER_RESUME_REQUIRED`，没有把临时页故障说成人机验证。 |
| F09 robots/network 分类 | 只有明确 robots 401/403 为 `ROBOTS_DENIED`；404、5xx、429、DNS/TLS/代理/网络错误为 unavailable/failed，不再误杀入口。 |
| F10 名称与来源治理 | 官方名称、生成名称、重复名、品牌和 marketplace scope 分开保存；文件名不再充当唯一 identity。 |
| F11 图片真实性 | 媒体阶段实际抓取字节、校验 magic/content type/size/hash，并按同一商品 gallery 顺序尝试下一个 URL；D bridge 实际查看了抓取的 JPG 文件，未使用推荐 SKU 或固定 PASS。 |
| F12 job/profile freeze | job 固化 profile version、snapshot、strategy/policy；只有显式 migration 才能换版本。 |
| F13 repeated cursor | 重复 cursor 记录为 `PAGINATION_UNVERIFIED` 并触发恢复，不直接标记 exhausted。 |
| F14 URL 边界 | public URL 严格校验、敏感 query/cookie/token 脱敏；bridge trace 只保留白名单证据，测试验证 `api_key`、`sk_`、cookie/token 不泄漏。 |

## 3. 真实 Codex bridge 证据

### 3.1 真实网页多轮闭环

证据目录：`E:\living\codex_bridge_live_site_20260905`。目标站点是 `https://www.interiordefine.com/`，不是 fixture URL。

实际闭环为：

1. `browse`：实际 HTTP 9019 bytes，robots 为 `ROBOTS_ALLOWED`；可见文本是 JavaScript-disabled 页面。
2. `escalate_browser`：实际创建/复用同一持久 browser session，返回 `BROWSER_RESUME_REQUIRED`、`TEMPORARY_PAGE_FAILURE` 和 resume token。
3. `inspect_product`：同一真实 PDP 返回无权威商品身份，明确保持空结果。
4. `list_product_images`：返回 `BROWSER_GALLERY`，没有候选就保持 `selected=null`。
5. `inspect_dimensions`：返回 `OFFICIAL_LOOKUP_INCOMPLETE`，包含 checked page 和 `official_absent=false`。
6. Codex bridge 最后调用 `finish`，以空 taxonomy 和真实原因停止；没有填充类别、数量、图片、尺寸或推荐 SKU。

最终 receipt 是 6 turns / 5 executed tools / 0 provider calls 的真实请求回路。此前发现的 inline analytics/API-key 泄漏风险已修复为只传 visible HTML；脱敏后的 pending trace 不含原始 secret。

### 3.2 图片证据与生产执行器

证据目录：`E:\living\codex_bridge_d_interior_20260905_run2`；对应生产池：`E:\living\website_acceptance_20260905_D_bridge_interior_run2`。

- B/C 通过真实 Interior Define Magento GraphQL/sitemap 证据产生 36 类目和 3 个首批商品；D 发现 9 个唯一候选。
- Codex bridge 收到每个候选的 URL、JPG 字节数、SHA-256、本地抓取路径和官方身份；实际用 `view_image` 查看了 Rowan、Maxwell、Graham、Celia、Skylar 等商品图，逐个返回结构化 `BrainProductDecision`。
- Maxwell 的真实官方尺寸候选进入 `MODEL_INPUT_LOCKED`；Provider OFF 阻止外部付费调用。
- 其它候选官方尺寸页返回 `TEMPORARY_PAGE_FAILURE`。第一次运行暴露了“有一个候选在推进时仍重复重试其它尺寸”的循环缺陷；修复后同一持久池恢复运行，真实输出为：

```text
exit_code=2
JOB_BLOCKED / DIMENSION_LOOKUP
blocker=TEMPORARY_PAGE_FAILURE
resume_safe=true
candidate=Rowan Bed
provider_calls=0
```

这证明是可恢复外部尺寸阻断，不是伪造的 READY，也不再拖到 watchdog 超时。

## 4. 45 站真实分层验收

### 4.1 运行证据与汇总

- A 全量运行：`E:\living\website_acceptance_20260905_A\acceptance_acceptance_20260905T050556Z.json`
- B/C 全量运行：`E:\living\website_acceptance_20260905_BC_final2\acceptance_acceptance_20260905T062535Z.json`
- D 定向运行：`E:\living\website_acceptance_20260905_D_interior\acceptance_acceptance_20260905T072459Z.json`；Provider OFF 且没有显式复核时保持 `LOCAL_AGENT_REVIEW_REQUIRED`。

A 层完整 45 站：`PASS 29 / ACCESS_BLOCKED 9 / FAILED 7`。

B 层（对 A 可继续的站点实际尝试）：`PASS 2 / PARTIAL 5 / ACCESS_BLOCKED 14 / ENVIRONMENT_BLOCKED 7 / UNSUPPORTED_CAPABILITY 1 / NOT_RUN 16`。

C 层：`PASS 1 / ENVIRONMENT_BLOCKED 6 / NOT_RUN 38`。唯一完整 C PASS 是 Interior Define，真实返回 3 个候选、12 个去重池商品证据；`image_pass_count=0` 是因为图片字节复核属于 D，而不是把 C 的 image URL 元数据冒充媒体 PASS。

### 4.2 逐站结果（A / B / C）

| 站点 | A | B | C | 备注 |
|---|---|---|---|---|
| Anthropologie | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | robots 明确拒绝 |
| Arhaus | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | 需同一可见浏览器 |
| Article | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Castlery | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| Interior Define | PASS | PASS | PASS | 3 个真实候选；D bridge 已实际走到尺寸阻断 |
| Ligne Roset | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | 需同一可见浏览器 |
| MacKenzie-Childs | PASS | PARTIAL | ENVIRONMENT_BLOCKED | 外部证据不完整 |
| Nathan James | PASS | PARTIAL | ENVIRONMENT_BLOCKED | 外部证据不完整 |
| POLYWOOD | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| RH | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | robots 明确拒绝 |
| Room & Board | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| Rove Concepts | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| Sixpenny | PASS | PARTIAL | ENVIRONMENT_BLOCKED | 外部证据不完整 |
| Walker Edison | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| India Art n Design | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Kayu | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| Fabuliv | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Globally Indian | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Indikasa | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Iris | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| Hometown | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Featherlite | PASS | PARTIAL | ENVIRONMENT_BLOCKED | 外部证据不完整 |
| Interio | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| Durian | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| Indian Nest | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Furnishka | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| LoveNspire | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| Indian Hub | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| DesignConnected | PASS | ENVIRONMENT_BLOCKED | NOT_RUN | TIMEOUTERROR |
| Archive3D | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| GrabCAD | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | robots 明确拒绝 |
| CGTrader | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Free3D | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | robots 明确拒绝 |
| Archibase | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| 3DExport | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | 需同一可见浏览器 |
| 3D Warehouse | PASS | UNSUPPORTED_CAPABILITY | NOT_RUN | `BRAIN_NOT_CONFIGURED`，未伪造类别 |
| Sweet Home 3D | PASS | PASS | ENVIRONMENT_BLOCKED | C 环境阻断 |
| NASA 3D | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| 3DXO | FAILED | NOT_RUN | NOT_RUN | A 失败 |
| MyMiniFactory | PASS | PARTIAL | ENVIRONMENT_BLOCKED | 外部证据不完整 |
| Poly Haven | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Alessi | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |
| Safavieh | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | robots 明确拒绝 |
| Driade | ACCESS_BLOCKED | NOT_RUN | NOT_RUN | 需同一可见浏览器 |
| West Elm | PASS | ACCESS_BLOCKED | NOT_RUN | BROWSER_REQUIRED |

`ACCESS_BLOCKED` 只表示当次真实入口要求 browser/robots/access change；不是把 CAPTCHA、临时失败或 DNS 错误混成同一类。原始 JSON/CSV 保存在上述受控目录，没有把浏览器 profile、cookies、商品图片或运行数据库提交到仓库。

## 5. 测试与本地网站 smoke

已执行并通过：

- `pytest -q services/api/tests/test_final_iteration_contract.py services/api/tests/test_domain_agent_v3.py services/api/tests/test_production_convergence.py`：通过。
- `pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests --maxfail=1`：`[100%]`，exit 0。
- `python -m compileall -q services/api/app workers packages/workflow_core scripts`：exit 0。
- Web `npm run typecheck`：exit 0；`npm run lint`：exit 0（2 个既有 warning，无 error）；`npm run build`：exit 0。
- 本地真实启动 smoke：`GET /api/v1/health` 200（version `0.16.0`），`/`、`/sites` 和前端代理 `/api/v1/health` 均 200；服务随后已停止，没有遗留后台服务。

## 6. 本地验收门

| 门 | 状态 | 说明 |
|---|---|---|
| G1 基线完整 | PASS | V3 与远端有效增量均保留 |
| G2 关键缺陷 | PASS | F01–F14 有代码和回归证据；尺寸重试循环在真实 D 恢复中再次修复 |
| G3 真大脑桥 | PASS | 真实网页 6-turn bridge；D 真实图片、哈希、工具、执行器闭环 |
| G4 真实流程 | BLOCKED | Interior Define 走到 D 并真实恢复；其它技术结构在 B/C 受站点/网络/浏览器阻断，尚未形成多结构 A–D 完整 PASS |
| G5 全清单覆盖 | PARTIAL/BLOCKED | 45/45 A 实际尝试，B/C 有完整机器矩阵；多数站点因真实外部阻断没有继续到 D |
| G6 状态真实 | PASS | 未知、失败、阻断、未穷尽、未验证均保留原状 |
| G7 生产正确性 | PASS | identity/media/dimensions/naming/账本回归和实际媒体字节校验通过 |
| G8 员工可用 | PARTIAL | 空库启动、UI health/home/sites、刷新相关 API 回归通过；尚未完成人工浏览器下完整 refresh/cancel/resume/download 验收 |
| G9 工程验证 | PASS | Python、Web typecheck/lint/build、compile、diff check、敏感信息检查通过 |
| G10 公司接入准备 | PASS（实连 NOT_VERIFIED） | 统一 adapter、模式诊断、同样本入口完成；公司 API 无凭据/网络未实连 |

因此本轮**本地验收门整体未通过**，不是代码没有完成，而是 G4/G5/G8 仍有真实证据缺口。按照附件要求，本轮不把源码推送到原 `main`。

## 7. 公司 API 接入步骤（同样本入口）

公司机器只需在本地私密环境配置，不要把密钥贴到聊天或提交 Git：

```powershell
$env:WEBSITE_MODEL_MODE = "TEXT_BRAIN_PLUS_VISION"
$env:WEBSITE_BRAIN_API_KEY = "<company-brain-key>"
$env:WEBSITE_BRAIN_BASE_URL = "https://<company-brain-endpoint>/v1"
$env:WEBSITE_BRAIN_MODEL = "<text-model>"
$env:WEBSITE_VISION_API_KEY = "<company-vision-key>"
$env:WEBSITE_VISION_BASE_URL = "https://<company-vision-endpoint>/v1"
$env:WEBSITE_VISION_MODEL = "<vision-model>"
```

然后执行同一份入口，不换生产路径：

```powershell
$env:PYTHONPATH = "services/api;.;packages/workflow-engine/src"
python scripts/run_45_site_acceptance.py --layers-bc --layers-d --sites "Interior Define" --operation-timeout-seconds 180 --output-root E:\company_acceptance\interior_define
```

开发代理模式使用 `WEBSITE_MODEL_MODE=CODEX_DEVELOPMENT_BRIDGE` 与 `CODEX_BRIDGE_ROOT`，由 `scripts/run_codex_bridge_live_site.py` / `scripts/codex_bridge_respond.py` 处理同一请求队列。默认没有公司凭据时不会静默假装远程模型已配置；公司 API、视觉协议和公司网络均在本轮标记为 `NOT_VERIFIED`。

## 8. 后续必须完成的真实工作

1. 在允许的可见浏览器/公司网络下补齐 Arhaus、Ligne Roset、POLYWOOD 等 `BROWSER_REQUIRED` 站点；不绕过 WAF/CAPTCHA，不复制 cookies。
2. 重试 Kayu、Iris、Interio、Archive3D、Archibase、NASA 3D、3DXO 的 DNS/TLS/网络失败，并保留新的 run id。
3. 对至少两个不同技术结构的可访问站点完成真实 B–D，尤其要得到官方尺寸或明确可恢复的 browser evidence。
4. 用公司视觉 API 对同一 Interior Define media SHA-256 做双边 receipt 校验，再决定是否启用受授权的 Provider。
5. 通过真实浏览器完成 UI 的刷新、取消、恢复、Ready Pool 下载/数量说明后，再重新评估 G4/G5/G8。

本报告记录的是当前可核实状态；未通过的门没有被包装成“员工可用”或“公司上线验收通过”。
