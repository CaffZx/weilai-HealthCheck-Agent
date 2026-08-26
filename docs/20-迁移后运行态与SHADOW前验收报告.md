# 迁移后运行态与 SHADOW 前验收报告

> 验收日期：2026-08-03  
> 环境：巡检 MySQL 测试库  
> 结论：迁移后运行态健康，历史数据隔离通过；外部合同未冻结，仍保持 `INTERNAL_ONLY`

## 1. 验收对象

- 迁移版本：`legacy-sqlite-v2`；
- Batch：`legacy-batch-70bed5d42b929c2107a2e865`；
- Job 699、Run 12,065、Signal 10,590、Occurrence 12,065；
- Fact Snapshot、Delivery Outbox、Feedback Inbox 均为 0。

## 2. 发现与修复

首次 v1 运行态检查发现 1,233 条迁移 Signal 的旧 `下一次复查时间` 已到期。当前开关虽然能
阻止执行，但未来启用 Scheduler 后可能集中生成历史复扫 Job。v1 批次经严格验证后完整回滚，
迁移器升级为 v2：旧时间仅保存在首条 Occurrence 的审计快照，运行态
`next_inspection_at` 固定为空，`verify` 新增 `MIGRATED_SIGNAL_SCHEDULING_LEAK` 门禁。

## 3. 运行态验收

| 验收项 | 结果 |
|---|---|
| 健康快照 | `HEALTHY`，数据库可用，无告警 |
| Rollout | `INTERNAL_ONLY` |
| Feature Flags | Scheduler、Result Delivery、Feedback 全部关闭 |
| Job | `SUCCEEDED=699`，`PENDING/RUNNING/DEAD=0` |
| Worker 领取 | 实际调用 `claim` 返回空，不会领取迁移 Job |
| 到期调度 | `due_signals=0`，到期经营单元为 0 |
| Outbox / Feedback | 均为 0，无历史补投或回传残留 |
| 预检 | Schema `0003_feedback_inbox_decoupling`，`HEALTHY / INTERNAL_ONLY` |

## 4. 查询与反馈隔离

- 历史 Run 可读取 Job、Batch、状态、触发类型和信号数，Fact Snapshot 与 Delivery 正确为空；
- 历史 Signal 可读取完整 `amazon_ops.inspection.v2` 合同和 Occurrence 历史；
- Feedback API 在当前阶段返回 `503 FEEDBACK_ROLLOUT_BLOCKED`；
- 调用前后 Feedback 数、Signal 版本总和与 Occurrence 数完全不变，证明阻断发生在消费前。

## 5. 迁移一致性

- 首次 v2 `apply`：`APPLIED + VERIFIED`；
- 二次 v2 `apply`：`ALREADY_APPLIED + VERIFIED`；
- 独立 `verify`：数量、Signal ID/版本/最后 Run、Occurrence 身份全部一致；
- `schedulable_signals=0`，测试库保留完整 v2 批次。

## 6. SHADOW 门禁结论

数据库迁移和内部运行态已经满足 SHADOW 前技术门禁，但当前不能切换阶段。仍需冻结 MCP
父级身份与事实字段合同、纯 `InspectionResultEnvelope` Result Sink 合同和 Feedback Event
合同，并完成真实 MCP—巡检—中控—Feedback 端到端联调。上述事项完成并签字前，Scheduler、
Result Delivery、Feedback 必须保持关闭，`rollout.stage` 必须保持 `INTERNAL_ONLY`。
