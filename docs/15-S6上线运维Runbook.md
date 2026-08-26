# S6 上线运维 Runbook

## AZListing Tool 变更门禁

V2 主源固定为 AZListing，`mcp.tool_policy` 必须保持 `AUDITED_ONLY`。当前允许调用 12 个 Tool：
两个互斥的经营单元 Tool 和 `clients/azlisting_contract.py` 中的 10 个事实 Tool。默认策略
默认 `PRINCIPAL_DIRECTORY` 组合调用用户目录、店铺目录和 #9；`QUERY_PAGE` 仅作为兼容备用策略。

#9 策略从环境变量 `AZLISTING_PRINCIPAL_SCOPES_JSON` 读取权威范围，每项必须含
`principalName/principalUserId/shopId/shopAccount/siteCode`。映射不得从旧 SQLite 生成；空映射、非法 JSON 或
同一 `shopAccount` 对应不同店铺/站点都会使预检失败。

新增或替换 Tool 时不得只改配置，必须同时完成：

1. 冻结入参、响应、身份、时区、金额/比例、分页、错误码和 SLA；
2. 更新 `clients/azlisting_contract.py`、`FactCollector`/Provider 与归一化逻辑；
3. 增加合同、单元和真实只读验收；
4. 确认敏感字段不参与归一化、哈希或 MySQL 持久化；
5. 重新执行 `runtime_preflight`。未审计 Tool 会以 `MCP_TOOL_NOT_ALLOWED` 拒绝，禁止临时放开任意调用。

## Streamable 补充事实启用门禁

补充网关不是 AZListing 的自动 fallback。销售与月度目标已由 AZListing 的
`erp_listing_gross_profit_history` 和 `erp_listing_monthly_goal` 统一提供；`sales_performance`
已退役。当前仅保留 `az_extend_detail` 作为 Seller ID/负责人降级来源，其余补充工具禁用。

## 子体异步事实

正式巡检只读取 `t_patrol_child_fact_snapshot` 中未过期的成功快照，并把缺失的子体事实写入
独立的 `t_patrol_child_fact_task`；不会在 `patrol.run` 中同步等待最多 80 个子体。

低并发 Worker 启动命令：

```bash
python -m integrations.runtime_child_fact_worker_main
```

默认每次工具调用至少间隔 0.25 秒，失败后冷却 15 分钟，最多重试 3 次，成功快照有效期 24 小时。

## 类目首次基准

首次观察到的子体类目以 `PENDING_CONFIRMATION` 写入 `t_patrol_category_baseline`，确认前类目异常
始终未判定。人工审核后执行：

```bash
python -m scripts.confirm_category_baseline <operating_unit_id> <child_asin> --confirmed-by <审核人>
```

单个 Tool 只有完成字段口径签字、真实样本回归、SLA/错误码确认、字段白名单复核并把合同状态
改为 `FROZEN` 后，才能加入 `enabled_tools`。运行预检会拒绝未知 Tool、草案 Tool、缺少 Token、
非 `FROZEN_ONLY` 策略，以及 `INTERNAL_ONLY` 下启用补充网关。

> 日期：2026-08-03  
> 当前阶段：`INTERNAL_ONLY`  
> 结论：上线底座已实现；外部合同未冻结前禁止进入 `CANARY/FULL`

## 1. 运行组件

| 组件 | 入口 | 副本建议 |
|---|---|---|
| API | `./start.sh` | 2+，无业务启动任务 |
| Scheduler | `python -m integrations.runtime_scheduler_main daily/fallback/due` | 1+，由 MySQL 租约保证单持有者 |
| Patrol Worker | `python -m integrations.runtime_worker_main` | 按 MCP 限流配置扩容 |
| Result Delivery Worker | `python -m integrations.runtime_delivery_main` | 1–2，依赖 Outbox `SKIP LOCKED` |

## 2. 灰度阶段

| 阶段 | Scheduler 范围 | 投递 | 反馈 | 进入条件 |
|---|---|---|---|---|
| `INTERNAL_ONLY` | Scheduler、人工 API、Patrol Worker 均禁止执行 | `SUPPRESSED`，永不投递 | 关 | 当前默认 |
| `SHADOW` | Scheduler、人工 API、Patrol Worker 仅允许店铺白名单 | `SUPPRESSED`，永不投递 | 强制关闭 | 运行枚举身份合同冻结、影子对比方案确认 |
| `CANARY` | Scheduler、人工 API、Patrol Worker 仅允许店铺白名单 | 仅白名单 Outbox | 开 | 结果/反馈合同、鉴权、告警和人工值守确认 |
| `FULL` | 全部有效经营单元 | 开 | 开 | Canary 稳定一个完整观察周期 |

`SHADOW/CANARY` 必须配置 `rollout.allowlisted_shop_ids`。人工巡检、复盘、Patrol Worker 和
CANARY Delivery Worker 都执行同一份白名单；历史越界 Job/Outbox 保持原状态，不会被领取。
`FULL` 必须同时启用 Scheduler、Result Delivery 和 Feedback；预检会拒绝不完整配置。

`INTERNAL_ONLY/SHADOW` 产生的 Outbox 初始状态均为 `SUPPRESSED`；投递 Worker 永远不会领取，
后续切换到 Canary 也不会补投历史内部或影子结果。`INTERNAL_ONLY` 下人工巡检返回 503，
Patrol Worker 即使被误启动也不会领取 Job 或访问 MCP。

V2 完全弃用 SQLite，唯一运行数据库为 MySQL。旧读写 API 均不再注册，并由永久退役中间件
统一返回 `410 SQLITE_RETIRED`；不存在 Feature Flag 或环境变量旁路。旧同步、补数、负责人、
覆写、盘点、备份、恢复和 SQLite→MySQL 迁移命令均在接触文件或外部系统前无条件拒绝。
历史源码和 `docs/17`、`docs/18` 仅作审计留存，不得作为当前操作手册。

部署调度分别在 `02:00` 执行 `python -m integrations.runtime_scheduler_main daily`、`07:00`
执行 `python -m integrations.runtime_scheduler_main fallback`。两者使用相同业务日、scope hash 和规则版本生成幂等键，
兜底命中既有批次时只返回原 `batch_id`，不会创建第二批 Job。到期复扫每 15 分钟执行
`python -m integrations.runtime_scheduler_main due`。

## 3. 上线前检查

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.runtime_preflight
.venv/bin/python -m scripts.verify_mysql_schema
./scripts/verify_s0.sh
```

预检是只读操作，会验证环境变量、Feature Flag、灰度阶段、Schema revision 和运行态健康。
默认不接受 `DEGRADED`；经负责人确认后才可临时使用 `--allow-degraded`。

不得在 V2 运维流程中执行任何 SQLite 盘点、备份、恢复或迁移。历史数据已经进入 MySQL 的，
按 MySQL 数据治理处理；未进入 MySQL 的旧 SQLite 数据不再由 V2 接收。

## 4. 监控与告警

- JSON 健康：`GET /api/v1/patrol/health`
- Prometheus：`GET /api/v1/patrol/metrics`
- `CRITICAL`：DEAD Job、DEAD Outbox、Worker 租约过期、数据库不可用；
- `DEGRADED`：Job/Outbox 积压或等待超时、近 24 小时反馈拒绝、Run 失败；
- 必须对 `patrol_health_status >= 2` 立即告警，对 `>= 1` 持续 15 分钟告警。

## 5. 反馈安全

Feedback 使用独立 `INSPECTION_FEEDBACK_TOKEN` Bearer 鉴权，不复用 MCP、Result Sink 或
历史中控 Token。Feature Flag 关闭时返回 503；开启但鉴权失败时返回 401，事件不会消费。

## 6. 回滚条件和动作

满足以下任一项立即停止放量：重复批次/活动信号、身份串号、规则不可解释漂移、Outbox
持续积压、非法生命周期、假恢复、MySQL 精度或截断错误。

回滚顺序：

1. `rollout.stage` 改为 `INTERNAL_ONLY`；
2. 关闭 `scheduler_enabled`、`result_delivery_enabled`、`feedback_enabled`；
3. 停 Scheduler 和 Delivery Worker，保留 API 只读诊断；
4. 不删除 MySQL 数据，不修改已冻结 Outbox payload；
5. 不覆盖旧 SQLite 备份，不恢复旧库写入，除非另有审批方案；
6. 导出健康快照、DEAD 记录和相关 Run/Occurrence 进行复盘。

## 7. 当前阻断

- MCP 运行枚举必须采用“#30 提供可信父身份”或“#9 + 非 SQLite 权威负责人/店铺/站点映射”；
- 结果接收端必须接受纯 `InspectionResultEnvelope` 并返回正式业务 ACK；
- 反馈合同需确认事件、原因码、版本和观察时间语义；
- 运维需确认部署副本、告警接收人、密钥中心和联调环境。

这些阻断解除前，项目只能保持 `INTERNAL_ONLY`，不能宣告生产闭环上线。
三方字段、样例和验收签字项见 `docs/16-外部合同冻结与E2E联调清单.md`。

## 8. 实施验证

| 验证项 | 结果 |
|---|---|
| S6 定向测试 | API、复盘、Patrol/Delivery Worker、Feedback 与阶段白名单均有防绕过测试 |
| 全量离线 | 393 项收集，387 例通过，另有 6 个真实 MySQL 用例按设计跳过 |
| MySQL 只读预检 | revision `0005_patrol_review`、`INTERNAL_ONLY`；健康状态按当次输出判断 |
| MySQL Schema | 8.0.46、13 张表；测试库保留联调和历史验证记录 |
| MySQL V2 工作台 | `/ui`、概览、批次、Job、Run、Signal 和事实元数据接口均通过开发测试库只读验收 |
| 真实 MySQL 写入测试 | 本次健康检查保持只读，6 项写入验证按设计跳过 |
| 合同漂移、Ruff（排除只读 `参考资料/` 归档）、compileall、凭据与 `t_ops_*` 写入扫描 | 通过 |
| 当前测试库只读数据 | 数量以 `/api/v1/patrol/health` 当次快照为准，不在 Runbook 固化动态计数 |
