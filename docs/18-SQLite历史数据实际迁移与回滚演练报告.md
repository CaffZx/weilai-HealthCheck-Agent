# SQLite 历史数据实际迁移与回滚演练报告

> **历史归档，禁止重跑或回滚。** 2026-08-04 起 V2 唯一数据库为 MySQL，SQLite 迁移 CLI
> 已永久关闭；已导入的 MySQL 数据按 MySQL 数据治理，不再以 SQLite 为恢复来源。

> 演练日期：2026-08-03  
> 环境：巡检 MySQL 测试库  
> 最终状态：完整历史迁移已保留，严格验证通过

## 1. 输入与边界

- 源文件为一致性 SQLite 备份，SHA-256：
  `7de1d0964f9b01813465357a9bea2b3e8d533837eb2db2193271858adfc1bd1c`；
- 身份映射文件 SHA-256：
  `f56bee4ef47bc754a851cd755bbb046cf52ad33ab36a9ce1c39594e8688220a3`；
- 迁移版本：`legacy-sqlite-v2`；
- 迁移 ID：`70bed5d42b929c2107a2e86546658291204768d5bf2241f45975b8780987c984`；
- Batch ID：`legacy-batch-70bed5d42b929c2107a2e865`；
- 未读取或修改线上 SQLite，未迁移旧事实缓存、任务权限、审批执行、旧投递或复盘职责；
- 未生成 Fact Snapshot 或 Delivery Outbox，历史结果不会被未来 Delivery Worker 补投。

## 2. 映射策略

- 一个历史迁移 Batch，`trigger_type=MANUAL`，清单保存源/映射哈希和期望数量；
- 每个经营单元创建一个 `LEGACY_MIGRATION` Job，共 699 个；
- 每条 `event_state_log` 创建确定性 Run 和 Occurrence，共 12,065 组；
- 每个 `event_pool` 创建完整可反序列化的 `InspectionSignal v2`，共 10,590 个；
- 历史经济暴露无法可靠换算，明确写为 `UNASSESSED`，不伪造 0；
- 历史证据未按 V2 MCP 合同重采，因此 Diagnosis 明确保留该不确定性；
- 历史日志类型保留为 `DETECTED`、`LEGACY_OPERATION`、`LEGACY_OBSERVATION`、
  `LEGACY_SEVERITY_CHANGE`，原日志 ID、状态、原因、操作人和上下文保存在 `details_json`；
- 旧 `下一次复查时间` 保存在首条 Occurrence 的 `legacy_event_snapshot` 中用于审计，
  不写入运行态 `next_inspection_at`；迁移信号只有收到新 Feedback 后才能进入复扫调度；
- 所有 ID 由迁移版本、源 SHA、映射 SHA 和旧主键确定性派生，重复执行不会创建新行。

## 3. 实际导入与对账

| 对象 | 数量 |
|---|---:|
| Batch | 1 |
| Job | 699 |
| Run | 12,065 |
| Signal | 10,590 |
| Occurrence | 12,065 |
| Fact Snapshot | 0 |
| Delivery Outbox | 0 |

Signal 状态为：`NEW 9,184`、`AWAITING_RESCAN 1,375`、`OBSERVING 21`、
`IN_PROGRESS 10`。Occurrence 类型为：`DETECTED 10,590`、`LEGACY_OPERATION 1,422`、
`LEGACY_SEVERITY_CHANGE 32`、`LEGACY_OBSERVATION 21`。

提交后检查全部数量、状态分布、10,590 个 Signal ID/版本/最后 Run 和 12,065 个
Occurrence ID/Signal/Run/类型均与源数据集精确匹配；抽验 100 个 Signal 均可被当前
`InspectionSignal` 合同反序列化。严格校验同时确认 `schedulable_signals=0`。

## 4. 幂等与回滚演练

1. 首次完整 `apply` 返回 `APPLIED + VERIFIED`；
2. 二次 `apply` 返回 `ALREADY_APPLIED + VERIFIED`，数量不变；
3. 独立 `verify` 返回 `VERIFIED`；
4. 完整 `rollback` 前确认无 Feedback、Outbox、外部 Occurrence 或 Signal 版本变化；
5. 回滚按 Occurrence、Signal、Run、Job、Batch 逆序删除，7 类关联计数全部为 0；
6. 回滚后再次完整 `apply` 和严格 `verify` 通过，测试库保留迁移完成状态；
7. 自动化测试额外验证：修改迁移 Signal 版本后，回滚必须拒绝。

首次 v1 导入后的运行态验收发现 1,233 条旧复查时间已到期。虽然当时 Scheduler 被
`INTERNAL_ONLY` 和 Feature Flag 双重关闭，但未来开启后可能集中触发历史复扫。该批次经
严格验证后完整回滚并升级为 v2；v2 保留旧时间审计信息，但从新运行调度面隔离。

## 5. 验证结果

- 迁移器单元测试：3 项通过；
- 迁移器真实 MySQL 集成测试：1 项通过；
- 最新全量离线回归：316 通过、7 个真实 MySQL 用例按设计跳过；
- 真实 MySQL 最近成功基线：313/313；新增 MCP 兼容测试后的复检因测试库连接超时待补跑；
- Ruff、compileall：通过；
- 当前测试库仅保留本次历史迁移业务数据，临时测试批次均已清理。

## 6. 后续门禁

本报告只证明测试库迁移与回滚机制可用，不授权正式库导入。正式切换前必须重新制作临近
切换时间的一致性 SQLite 备份，重新校验 SHA、重新执行身份预演，并完成变更审批、停写或
双写边界确认、备份保留期、回滚窗口和值守安排。外部 MCP、Result Sink、Feedback 合同和
真实 E2E 门禁仍未解除，当前 rollout 必须保持 `INTERNAL_ONLY`。
