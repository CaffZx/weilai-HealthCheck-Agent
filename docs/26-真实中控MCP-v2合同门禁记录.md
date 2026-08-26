# 真实中控 MCP v2 合同门禁记录

检查日期：2026-08-05  
目标工具：`submit_patrol_batch`  
检查方式：仅执行 MCP `initialize` 与 `tools/list`，未调用 `tools/call`，未写入中控业务数据。

## 检查结果

- MCP 连接与 Bearer 鉴权成功；
- `submit_patrol_batch` 工具存在；
- 实时输入 Schema 顶层包含 `patrolBatchNo`、`units`；
- 完整实时 Schema 不包含 `proposal.anomalies`；
- 完整实时 Schema 不包含 `proposal.details[].anomalyUid`；
- 实时 Schema SHA-256：`50fdec0e1498b90afa189d7b4738e85a139631eda751fec88e5758da6e9b49ad`。

结论：中控当前部署的工具合同不是《巡检Agent_MCP接收巡检数据接口文档_v2.0.md》要求的最新合同。巡检 Agent 已按失败关闭原则停止真实业务提交，避免把 v2.0 载荷发给旧合同或造成错误落库。

## 中控升级验收条件

中控部署后重新执行 `scripts/check_control_center_schema.py`，以下四项必须全部为 `true`：

```text
anomalies_present=true
anomalies_required=true
detail_anomaly_uid_present=true
detail_anomaly_uid_required=true
```

通过门禁后，再使用同一巡检业务批次号生成并提交 20 个经营单元的 v2.0 载荷。重试时保持该批次号与经营单元业务键不变。

## 二次复检

2026-08-05 再次读取真实中控 `tools/list`：Schema 指纹未变化。`units.items` 已声明
`listing/tags/operatingMetric/factSnapshots/proposal`，但 `proposal` 仍只要求 `details`，
其余字段使用 `additionalProperties`；实时合同仍未声明并强制校验 `anomalies` 和
`details[].anomalyUid`。本次仍未调用 `tools/call`。

## 运行时调用勘误

随后按最新 v2.0 文档使用一条真实经营单元执行 `tools/call` 探针。中控运行时成功接收
`proposal.anomalies` 和 `proposal.details[].anomalyUid`，返回 `SUCCESS/OK`。因此准确结论是：

- 中控 `tools/list` 暴露的输入 Schema 落后于实际运行时能力；
- 运行时 `submit_patrol_batch` 已能够接收当前 v2.0 载荷；
- Schema 发布仍应由中控修复，避免客户端代码生成、合同发现和自动门禁误判。

巡检侧已在探针成功后使用同一 `patrolBatchNo` 提交剩余经营单元，正式结果见
《27-20个真实经营单元中控MCP投递报告》。
