# MySQL 测试库部署报告

> 日期：2026-07-31  
> 环境：巡检测试库  
> 状态：部署及验证通过  
> 实施基线：`docs/07-巡检AgentV2最终改造实施方案.md`

## 部署结果

| 项目 | 结果 |
|---|---|
| 数据库地址 | `10.0.0.0:3306/erp_example` |
| MySQL 版本 | 8.0.46 |
| 数据库排序规则 | `utf8mb4_bin` |
| SQL 模式 | 包含 `STRICT_TRANS_TABLES` |
| Alembic revision | `0003_feedback_inbox_decoupling` |
| 巡检表 | 11 张，均为 InnoDB、`utf8mb4_bin` |
| 外键 | 8 个（Feedback Inbox 对未知 Signal 的拒绝事件也必须可审计） |
| CHECK 约束 | 10 个 |
| 业务表初始状态 | 空，无验证残留 |

本次仅创建 `t_patrol_*` 表，不写入、不修改 `t_ops_*`。数据库口令仅通过进程环境变量
传入，没有写入仓库文件或本报告。

## 已创建表

1. `t_patrol_schema_version`
2. `t_patrol_batch`
3. `t_patrol_job`
4. `t_patrol_run`
5. `t_patrol_fact_snapshot`
6. `t_patrol_signal`
7. `t_patrol_signal_occurrence`
8. `t_patrol_feedback_inbox`
9. `t_patrol_delivery_outbox`
10. `t_patrol_idempotency`
11. `t_patrol_scheduler_lock`

## 验证结果

| 验证项 | 结果 |
|---|---|
| Schema、版本、引擎及排序规则 | 通过 |
| 唯一约束拒绝重复幂等键 | 通过 |
| CHECK 约束拒绝非法聚合计数 | 通过 |
| `FOR UPDATE SKIP LOCKED` | 通过 |
| 验证事务回滚及无数据残留 | 通过 |
| MySQL 迁移静态测试 | 2/2 通过 |
| MySQL Repository 原子事务测试 | 2/2 通过 |
| S2 队列、租约与生命周期真实库测试 | 2/2 通过 |
| S3 纯巡检主链路真实库测试 | 1/1 通过 |
| S5 Outbox、ACK、Inbox 与到期复扫真实库测试 | 1/1 通过 |
| 金额 `DECIMAL(18,4)` 与 `ROUND_HALF_UP` | 通过，`12.34565` 保存为 `12.3457` |
| 全量测试 | 离线 251 项通过、5 项真实 MySQL 测试跳过；注入 URL 后 256/256 通过 |
| S0 统一质量门 | 通过 |

复验时由操作者在当前终端设置 `PATROL_DATABASE_URL`，再执行：

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.verify_mysql_schema --expect-empty
.venv/bin/python -m scripts.verify_mysql_behavior
.venv/bin/python -m scripts.verify_mysql_repository
.venv/bin/python -m scripts.verify_mysql_s5
./scripts/verify_s0.sh
```

`--expect-empty` 仅适用于尚未写入业务数据的测试库。开始联调后，应移除此参数，避免把
正常业务数据误判为部署失败。

## 回滚

回滚命令：

```bash
.venv/bin/alembic downgrade base
```

该操作会删除全部 11 张 `t_patrol_*` 表及其中数据，属于破坏性操作。执行前必须确认目标
仍为测试库，并完成所需备份；本次部署验证没有执行回滚。

## 边界

本报告确认数据库运行态底座及 S1–S5 内部数据库链路已验证。自动 Scheduler 仍受 MCP
运行枚举身份合同阻断；中控投递和反馈尚缺正式外部合同与联调环境，不能因内部实现通过而
视为整套 V2 已上线。
