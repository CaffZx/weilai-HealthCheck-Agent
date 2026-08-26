# S1 合同与 MySQL 实施报告

> 日期：2026-07-31  
> 状态：S1 退出条件通过  
> 实施基线：`docs/07-巡检AgentV2最终改造实施方案.md`

## 交付结果

- 新增 `InspectionRun` 正式合同及收敛运行状态；
- 新增纯巡检 `InspectionResultEnvelope`，仅输出 Run、Signal、Handoff 和生产者版本；
- 新增 `InspectionFeedbackEvent` 合同，供 S5 Inbox 消费使用；
- 经济暴露金额统一为 `Decimal`，四位小数并使用 `ROUND_HALF_UP`；
- 新增 SQLAlchemy 2 Core Repository 和显式 Unit of Work；
- 实现 Run、FactSnapshot、Signal、Occurrence、Outbox 同事务保存；
- 新增运行记录查询，并对重复请求、引用漂移和重复业务键显式报错；
- 导出三份新 JSON Schema，并纳入合同漂移检查。

## 原子事务验证

测试库执行以下场景并全部通过：

1. 在同一事务创建运行记录并保存 Snapshot、Signal、Occurrence 和 Outbox；
2. 事务内五类记录同时可见，退出 Unit of Work 后全部回滚；
3. 人为制造第二个相同业务键 Signal，数据库拒绝后整笔事务无残留；
4. 金额 `12.34565` 按统一规则保存为 `12.3457`；
5. 测试结束后 10 张业务表保持空，Schema revision 仍为 `0001_patrol_runtime`。

复验命令由操作者临时设置 `PATROL_DATABASE_URL` 后执行：

```bash
.venv/bin/pytest tests/integration/test_mysql_repository.py -q
.venv/bin/python -m scripts.verify_mysql_repository
.venv/bin/python -m scripts.verify_mysql_schema --expect-empty
./scripts/verify_s0.sh
```

## 测试结果

| 门禁 | 结果 |
|---|---|
| 合同测试 | 55/55 通过 |
| R2/R3/R7 回归 | 20/20 通过 |
| 全量测试集合 | 208 项 |
| 离线基线 | 206 项通过，2 项真实 MySQL 测试按设计跳过 |
| 真实 MySQL Repository | 2/2 通过 |
| Schema 与行为复验 | 通过，且无测试数据残留 |

## 边界与下一阶段

S1 完成只表示领域合同和 MySQL 持久化底座可用。当前 Repository 对新 Signal 执行首次
插入；持续、恢复、复发和缺口对账属于 S2 `SignalReconciler`，不会在 S1 提前实现。
Scheduler、Worker 任务领取、租约恢复、正式 MCP Adapter、Result Sink 和 Feedback Inbox
也分别属于 S2、S4、S5，尚不能宣布整套 V2 闭环完成。
