# 中控双向 MCP 联调实施说明

## 1. 职责边界

巡检 Agent 负责事实采集、巡检分析、建议组装、复盘事实重采集、复盘判定和结果持久化。
中控只负责接收巡检结果、保存与展示结果、发起复盘请求、接收复盘结果；中控不得向
巡检传入巡检结论或复盘结论。

## 2. 两条交互链路

### 2.1 巡检 Agent → 中控

- 协议：Streamable HTTP MCP；
- 中控 Tool：`submit_patrol_batch`；
- 巡检侧客户端：`clients/control_center_mcp.py`；
- 请求模型：`SubmitPatrolBatchRequest`；
- 返回模型：`SubmitPatrolBatchResult`；
- 中控地址格式：`http://{host}:8756/mcp`，真实地址和 Token 仅从环境变量读取；
- `SUCCESS` 和 `PARTIAL_SUCCESS/PROPOSAL_LOCKED` 均视为中控已接收；
- 必须逐项检查 `results[]`，不得以 HTTP 200 判定整批成功。

当前 V2 的 `InspectionResultEnvelope` 是内部巡检信号合同，不可直接伪装成
`submit_patrol_batch`。中控要求的 `operatingMetric`、`factSnapshots` 和 `proposal` 必须由
巡检 Agent 的完整分析链路真实生成后，再组装为中控载荷。

完整 `SubmitPatrolBatchRequest` 由 `ControlCenterBatchPublisher` 原子写入 MySQL
`t_patrol_delivery_outbox`，类型固定为 `CONTROL_CENTER_PATROL_BATCH`。独立投递 Worker
使用冻结的 `payload_json` 原包重试，不重新巡检或重新生成建议。内部纯巡检 Envelope 和
中控载荷由不同 Worker 消费，不能相互转换。当前主巡检链尚未生成全部中控必填业务字段，
因此只提供完整载荷发布入口，不自动排队不完整结果。

当前回传合同已按中控 v2.0 文档更新：`operatingMetric` 使用 `metricDate` 的每日指标模型，
广告花费字段为 `adCost`；`tags` 对象必须存在，但字段均可省略；经营模式和建议状态
使用固定枚举；`proposal.anomalies` 和 `proposal.details` 均至少一条，每条建议明细必须通过
`anomalyUid` 引用同一建议中的结构化异常。指标幂等口径为“经营单元业务键 + `metricDate`”，
新批次会使同经营单元的旧建议进入 `EXPIRED`。脱敏请求样例见
`contracts/examples/control-center-submit-patrol-batch.v2.example.json`，共享合同见
`contracts/control-center-submit-patrol-batch.v2.schema.json`。原 v1 Schema 已退出当前导出清单，
不得再用于新请求校验。

首次回传的完整载荷还必须满足以下一致性约束：

- `shopId` 和负责人集合必须来自权威身份目录。AZListing
  `erp_amazon_listing_query_page.records[]` 的 `shopId/userId` 会按经营单元聚合并冻结到任务；
  同一经营单元允许多个负责人，不得按负责人拆分经营单元，也不得随机选择一个人；
- 中控 v2.0 当前只有可选单值 `ownerUserId`。负责人集合恰好一人时可直接映射；多人时不得
  随机选择，可暂不传该字段，等待中控冻结“主负责人”规则或升级为 `ownerUserIds[]`；
- 每个经营单元至少包含一条事实快照；只强制 `snapshotType + sourceSystem` 唯一。时间窗、
  内容哈希、质量、完整度、关键指标和原始查询引用均按 v2.0 作为可选字段接收；
- `proposal.anomalies[].anomalyUid` 在同一建议内唯一；`details[].anomalyUid` 必须命中同一
  `proposal.anomalies`。异常和建议明细各自使用文档冻结的类型、编码和状态枚举；
- 问题、根因、目标、预期效果、风险、观察期、复盘时间、原始分析以及建议明细的说明性字段
  均按 v2.0 作为可选字段接收；相同对象上的重复动作仍会在发送前拒绝；
- 经营模式、经营配置标签和专业建议属于上游数据源义务，不是巡检侧默认生成项。
  经营模式必须来自经营模式 Agent 或权威配置，配置标签来自经营单元配置，专业建议来自
  对应专业 Agent 或已冻结规则；当前没有价格 Agent，巡检不得生成可执行调价建议；
- `recommendedBusinessModel` 与 `modeReasonSummary` 均为可选字段；巡检从经营模式 Agent
  获得确定结果时会同时发送模式和理由，合同校验不额外强制二者绑定。

经营模式 Agent 已提供 Streamable HTTP MCP。巡检批量调用只读 Tool
`get_current_operating_modes`；未命中时启动 `start_operating_mode_evaluation_job`，轮询
`get_operating_mode_evaluation_job` 到终态后再次查询。巡检不会调用会执行判断并写库的
`evaluate_operating_mode`。最终结果为 `DECIDED` 时，巡检把权威模式和解释写入中控载荷；
返回 `BLOCKED`、`NEEDS_HUMAN_REVIEW` 或 `null` 时，允许使用同一经营单元的
`erp_listing_advert_agent_config.operatingMode` 作为显式保底值，但不得伪装成 OM Agent 的
`DECIDED`。该 Tool 要求调用方已知
经营单元业务键，因此它是经营模式来源，不是经营单元清单发现接口。

经营模式 Agent 的权威枚举 `ACTIVE_ADVANCE` 与中控 v2.0 枚举 `CONTROLLED_GROWTH` 名称不同。
巡检适配层只做这一项显式语义映射，并在 `rawAnalysis.operatingMode` 保留源枚举、源 Tool、来源类型和解释；
其余六种模式逐字映射。只读 Tool 不返回置信度，因此 `modeConfidence` 保持为空，不使用模拟值。

2026-08-04 真实只读验证：服务 `amazon-operating-mode-agent` 可完成初始化和 Tool Schema
发现；对巡检样本 `1562/B0EXAMPLE0`、`1596/B0EXAMPLE0`、`36451/B0EXAMPLE0`
查询均返回 `null`，表示当前尚未保存这些单元的经营模式，不是调用失败。

经营模式 MCP 停机期间可使用离线 Mock 服务完成合同联调。样例文件
`contracts/examples/operating-mode-mcp.v1.mock.json` 严格区分模拟身份与真实数据，包含
`evaluate_operating_mode` 的请求及 `DECIDED/BLOCKED` 响应，以及只读
`get_current_operating_mode` 的 `DECIDED/BLOCKED/NEEDS_HUMAN_REVIEW/null` 四种结果。
本地服务只暴露查询/任务状态 Mock，不实现 `evaluate_operating_mode`，也不连接 MySQL：

```bash
python -m scripts.mock_operating_mode_mcp
```

默认地址为 `http://127.0.0.1:8801/mcp`，测试 Token 可使用任意非空字符串（Mock 服务不做
鉴权）。联调查询键见样例文件；其中 `99001622/B0EXAMPLE0/DEMO-JUICER-P` 返回
`ACTIVE_ADVANCE`，巡检适配后应得到中控 `CONTROLLED_GROWTH`，且不得填充模拟置信度。
Mock 文件和服务只能用于本地合同联调，不得连接生产运行配置或作为经营模式事实来源。

仓库中的完整 JSON 仅是脱敏合同联调样例，其中 `SIM-*` 身份、指标、经营模式、标签和
广告建议均为模拟数据，不代表真实经营结论，也不能直接投产执行。

### 2.2 中控 → 巡检 Agent

- 协议：Streamable HTTP MCP；
- 巡检 Tool：`review_patrol_result`；
- 服务入口：`python -m integrations.review_mcp_server`；
- 中控输入：复盘请求号、轮次、批次号、建议号、巡检侧基线快照 ID、经营单元身份、
  已批准计划、执行回执和预先约定的成功/失败标准；
- 巡检处理：从 MySQL 读取基线，调用 AZListing MCP 采集当前事实，在巡检内部完成复盘；
- 巡检输出：当前事实快照、成功/失败标准逐项结果、执行偏差、最终复盘结论和下一步；
- 幂等键：`reviewRequestId + reviewRound`；相同键不同请求内容必须拒绝。

复盘 MCP 不使用旧 SQLite，不调用旧 `mcp_history` 服务，也不要求中控提供结果回调地址。
Tool 调用响应就是最终复盘结果。

## 3. 安全与启用门禁

- 入站复盘令牌仅使用 `REVIEW_MCP_PUBLIC_TOKEN`；不得复用事实源 `MCP_API_KEY`；
- 复盘 MCP 仅监听 `127.0.0.1:8791`，由 V2 Web `/mcp` 完成 Bearer 校验和转发；
- `REVIEW_MCP_ENABLED=false` 时 Tool 必须拒绝执行；
- 当前 `rollout.stage=INTERNAL_ONLY`、Scheduler、巡检 Worker 和中控结果投递继续关闭；
- `feature_flags.operating_mode_lookup_enabled=true` 已启用经营模式闭环；完整中控载荷在
  冻结 Outbox 前批量查询，未命中项自动启动评估任务、等待终态并再次查询。`DECIDED`
  才映射权威模式，任务或查询失败则不创建中控 Outbox；
- `INTERNAL_ONLY` 允许该能力待命，但 Scheduler、巡检 Worker 和中控投递仍关闭；
  配置只允许批量查询、自动评估任务和任务状态查询，手动 `evaluate_operating_mode` 始终禁止；
- 中控投递还需显式开启 `feature_flags.control_center_delivery_enabled`，并且仅在
  `CANARY/FULL` 阶段运行；
- 中控地址、Token 和真实样例冻结前，不执行真实外部写入。

## 4. 中控团队需确认

1. `submit_patrol_batch` 测试环境 `{baseUrl}/mcp` 和 Bearer Token；
2. 文档中的 Tool 输入、业务码和 `structuredContent` 返回结构与真实服务一致；
3. 复盘请求中的 `reviewRequestId`、`reviewRound`、`patrolBatchNo`、`proposalId`、
   `baselineSnapshotId` 是否可以原样保存并回传；
4. `approvedPlan.items[].planItemId` 与 `executionReceipts[].planItemId/platformStatus` 字段；
5. 成功/失败标准是否由首次巡检时确定并由中控原样回传；
6. 中控调用巡检复盘 MCP 的网络出口 IP、Bearer Token 配置方式和超时时间；
7. 中控接受同步 Tool 响应，还是要求异步任务状态查询。当前实现为同步响应。

## 5. 联调顺序

1. 双方 `initialize`、`tools/list` 和 Bearer 鉴权；
2. 中控使用脱敏样本调用 `review_patrol_result`，巡检侧使用 Fake MCP 完成合同联调；
3. 巡检侧使用脱敏样本调用中控 `submit_patrol_batch`；
4. 在测试 MySQL 与真实只读 AZListing MCP 上完成单经营单元复盘；
5. 验证重复请求、错误 Token、基线不匹配、数据不足和超时；
6. 完成真实“巡检 → 中控 → 复盘请求 → 复盘结果”闭环后，才评估 SHADOW。
