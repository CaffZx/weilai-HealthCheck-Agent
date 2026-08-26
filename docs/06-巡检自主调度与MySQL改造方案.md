# 巡检 Agent 自主调度与 MySQL 改造方案

> **历史设计稿。当前决策已覆盖本文的过渡方案：V2 完全弃用 SQLite，唯一运行数据库为
> MySQL；不保留只读观察、备份、恢复或迁移入口。**

> 状态：实施方案 v1.0  
> 日期：2026-07-31  
> 适用项目：`weilai-HealthCheck-Agent-v2.0`

## 1. 已确认的项目决策

本方案以以下决策为准，覆盖此前“由中控逐个触发巡检”的临时实现：

1. 巡检 Agent 自己管理日常巡检和到期复扫的触发。
2. 巡检 Agent 只把自己产生的巡检结论整理后交给下游。
3. 巡检 Agent 不负责经营模式决策、经营建议聚合、审批和执行。
4. 经营事实后续通过本项目封装的 MCP 工具获取；具体工具方案另行补充。
5. 巡检私有运行数据迁移到 MySQL 8，不直接写中控业务表。
6. 现有 R2、R3、R7 和运行配置继续作为规则基线，不重新设计巡检规则。

目标定位：

```text
自主触发巡检
→ 获取经营单元
→ 通过 MCP 获取事实
→ 校验并冻结事实快照
→ 运行确定性规则
→ 识别异常、机会、恢复、复发和数据缺口
→ 生成 InspectionRun + InspectionSignal v2
→ 保存巡检私有运行态
→ 可靠投递巡检结论
```

## 2. 职责边界

### 2.1 巡检负责

- 日常全量扫描、到期复扫和人工补跑；
- 经营单元身份校验；
- MCP 事实获取、归一化、时效与完整性校验；
- 事实快照及证据引用；
- R2 异常识别和 R3 严重度判定；
- 首次发现、持续命中、恢复、复发和数据缺口识别；
- `InspectionSignal v2` 四个确定性队列字段；
- 必要的 `HandoffDirective v2`；
- 幂等、任务恢复、失败重试、Outbox 投递和审计。

### 2.2 巡检不负责

- 决定或修改经营模式；
- 自动调用广告 Agent 产生广告方案；
- 生成价格、库存、Listing 或广告执行动作；
- 生成 `DecisionProposal` 或 `ActionOrder`；
- 审批、执行和修改中控业务状态；
- 把执行完成直接判断为经营结果成功；
- 直接写现有 `t_ops_*` 中控表。

## 3. 现有配置复用

### 3.1 原样保留

| 配置 | 当前值/内容 | 改造处理 |
|---|---|---|
| 每日巡检 | `02:00` 启动 | 保留 |
| 超时兜底 | `07:00` 兜底 | 保留，但改成幂等创建批次 |
| 时区 | `Asia/Shanghai` | 保留；数据库时间统一存 UTC |
| 批量大小 | `inspection.batch_size: 50` | 保留 |
| 巡检并发 | `inspection.concurrency: 8` | 保留 |
| 最大并行单元 | `patrol.max_concurrent_units: 20` | 保留 |
| 默认取数窗口 | `30` 天 | 保留 |
| 销售时效 | D0-D2 正常、D3 延迟 | 保留 |
| MCP 调用 | 超时 15 秒、并发 6、最多 2 次 | 保留，待 MCP 方案校准 |
| 任务重试 | 3 次 | 保留 |
| 重复巡检窗口 | 24 小时 | 保留并真正接入调度器 |
| LLM | 例行关闭，人工复杂诊断最多 1 次 | 保留 |
| R2 | 异常字典、作用层级和跳过关系 | 保留 |
| R3 | S0/S1/S2、样本不足和数据不足规则 | 保留 |
| R7 | 1/3/7/14/30 天复查规则 | 保留 |
| 信号排序 | 约束、时点、经济暴露、就绪度 | 保留 |
| 信号有效期 | S0 24h、S1 72h、S2 168h | 保留 |

每次批次和巡检运行必须记录实际使用的配置、R2、R3、R7 和队列规则版本，保证结果可重放。

### 3.2 退出新主链路

| 配置 | 处理 |
|---|---|
| `proposal_rules.yaml` | 经营建议和审批专用，不进入巡检主链路 |
| `R4_打分参数.yaml` | 旧综合分和 P0/P1/P2，仅保留历史兼容 |
| `R6_处理建议.yaml` | 不再用于生成执行建议；可迁移其中的责任能力映射 |
| `mode_agent` | 从新主链路断开 |
| `advertising_agent` | 从新主链路断开，只保留历史代码或人工交接适配 |

### 3.3 新增配置

在 `settings.yaml` 中只放非敏感参数和环境变量名称：

```yaml
database:
  driver: mysql+pymysql
  host_env: PATROL_MYSQL_HOST
  port_env: PATROL_MYSQL_PORT
  database_env: PATROL_MYSQL_DATABASE
  user_env: PATROL_MYSQL_USER
  password_env: PATROL_MYSQL_PASSWORD
  pool_size: 10
  max_overflow: 20
  pool_recycle_seconds: 1800
  connect_timeout_seconds: 5

scheduler:
  enabled: true
  timezone: Asia/Shanghai
  daily_at: "02:00"
  fallback_at: "07:00"
  due_scan_interval_minutes: 15
  lease_seconds: 120
  batch_size: 50

delivery:
  sink_type: http
  base_url_env: PATROL_RESULT_SINK_URL
  token_env: PATROL_RESULT_SINK_TOKEN
  max_retry: 10
  base_backoff_seconds: 60

features:
  use_mysql_runtime: true
  use_legacy_sqlite_read: true
  enable_legacy_batch_api: false
```

密码和 Token 只能通过环境变量或密钥中心注入，不进入仓库。

## 4. 触发设计

### 4.1 三种正式触发

| 类型 | 发起方 | 经营单元范围 | 说明 |
|---|---|---|---|
| `DAILY_SCHEDULE` | 巡检 Scheduler | 全部有效经营单元 | 每日 02:00 创建批次 |
| `OBSERVATION_DUE` | 巡检 Scheduler | 到期信号对应单元 | 每 15 分钟扫描到期项 |
| `MANUAL` | 巡检 API/管理端 | 指定单元或指定范围 | 人工补跑和问题复现 |

`CONTROL_CENTER` 不再作为日常主触发；`DATA_REPAIRED` 暂时保留枚举兼容，等数据修复事件方案确定后再启用。

### 4.2 每日批次

```text
02:00 Scheduler 获取领导者租约
→ 调用经营单元 MCP 获取有效清单
→ 生成 scope_hash 和 DAILY_SCHEDULE 批次
→ 按 batch_size=50 分片
→ 每个经营单元生成一条 patrol.run 任务
→ Worker 并发处理
→ 批次收尾：恢复识别、遗漏检查、统计和投递

07:00 兜底任务再次尝试创建同一业务日期批次
→ 命中唯一约束后返回原批次
→ 不产生第二轮重复巡检
```

每日批次幂等键：

```text
DAILY_SCHEDULE:{business_date}:{scope_hash}:{rule_bundle_version}
```

### 4.3 到期复扫

R7 继续计算 `next_inspection_at`。Scheduler 查询已到期且没有活动任务的信号，按经营单元合并创建复扫任务。同一经营单元同一时间只运行一轮巡检，一轮结果可以同时覆盖该单元多个到期信号。

### 4.4 人工补跑

保留 `POST /api/v1/patrol/runs`，但定位改为人工或运维补跑。请求必须包含 `X-Request-Id`；同键同内容返回原任务，同键不同内容返回 `409`。

旧 `POST /api/batch/inspect` 先标记 deprecated，再在 MySQL 主链路稳定后下线。

## 5. 新巡检主链路

将当前编排器缩减为以下步骤：

1. 校验任务、批次和经营单元身份；
2. 创建 `InspectionRun`，状态为 `COLLECTING_FACTS`；
3. 通过 MCP 获取所需事实；
4. 归一化、校验质量并生成不可变事实快照；
5. 核心事实不足时生成数据缺口信号，不产生经营动作；
6. 调用现有 `LegacyRuleAdapter` 执行 R2/R3；
7. 将内部异常转换为 `InspectionSignal v2`；
8. 与上一轮信号集合对账，识别新增、持续、恢复和复发；
9. 在一个数据库事务中保存 Run、信号和 Outbox；
10. Worker 完成任务；Delivery Worker 异步投递原始结论。

主链路不再调用：

- `ModeAgentClient.evaluate()`；
- `AdvertisingAgentClient.propose()`；
- `PatrolAnalyzer.analyze()` 中的经营建议聚合；
- `ProposalBuilder.build()`；
- `ReviewOrchestrator` 的经营结果判断。

## 6. MCP 接入边界

具体 MCP 工具尚未冻结，因此先建立两个稳定端口：

```python
class OperatingUnitProviderPort(Protocol):
    async def list_active_units(self, *, cursor: str | None, limit: int) -> UnitPage: ...

class FactProviderPort(Protocol):
    async def collect(self, unit: OperatingUnitBinding, *, as_of: date) -> RawFactBundle: ...
```

后续 MCP 方案只替换 Adapter，不改变 Scheduler、Worker、规则和数据库。

经营单元工具至少要返回：

- `shop_id`；
- `shop_account` 或等价取数绑定；
- `site_code`；
- `parent_asin`；
- `parent_seller_sku`；
- 是否有效、停售或归档；
- 数据版本或更新时间；
- 分页游标。

事实工具必须返回来源、事实截止时间、内容版本和错误类别；缺失值不能用默认值补齐。

## 7. MySQL 设计

### 7.1 数据库边界

测试环境已经存在 `t_ops_*` 中控表。巡检新增表统一使用 `t_patrol_*` 前缀，不直接修改或写入 `t_ops_*`。

测试环境当前为 MySQL 8.0.46、`utf8mb4`、`utf8mb4_bin`，严格模式开启，可以支持本方案。生产建议使用巡检独立数据库或仅有 `t_patrol_*` 权限的独立账号。

### 7.2 表清单

| 表 | 作用 | 核心唯一约束 |
|---|---|---|
| `t_patrol_batch` | 全量、到期或人工巡检批次 | `idempotency_key` |
| `t_patrol_job` | 单经营单元任务队列 | `(batch_id, operating_unit_id)` |
| `t_patrol_run` | 每次单元巡检运行记录 | `run_id`、`request_id` |
| `t_patrol_fact_snapshot` | 事实摘要、质量、来源和哈希 | `snapshot_id`、`content_hash` |
| `t_patrol_raw_fact` | MCP 原始请求、完整响应和复用血缘 | `(run_id, fact_key)` |
| `t_patrol_signal` | 当前信号生命周期 | 信号业务身份唯一键 |
| `t_patrol_signal_occurrence` | 每轮命中/未命中/恢复审计 | `(signal_id, run_id)` |
| `t_patrol_delivery_outbox` | 结论可靠投递 | `(aggregate_type, aggregate_id, aggregate_version)` |
| `t_patrol_idempotency` | 写接口幂等 | `(actor_id, idempotency_key, route)` |
| `t_patrol_scheduler_lock` | Scheduler 领导者租约 | `lock_name` |
| `t_patrol_schema_version` | 数据库迁移版本 | `version` |

### 7.3 关键字段

#### `t_patrol_batch`

```text
batch_id             VARCHAR(64) PK
idempotency_key      VARCHAR(191) UNIQUE
trigger_type         VARCHAR(32)
business_date        DATE
scope_json           JSON
scope_hash           CHAR(64)
rule_bundle_version  VARCHAR(128)
status               VARCHAR(24)
total_count          INT
pending_count        INT
running_count        INT
succeeded_count      INT
failed_count         INT
started_at           DATETIME(6)
finished_at          DATETIME(6) NULL
created_at           DATETIME(6)
updated_at           DATETIME(6)
```

批次状态：`CREATED/ENQUEUING/RUNNING/PARTIAL_SUCCESS/SUCCEEDED/FAILED/CANCELLED`。只有全部任务成功才允许 `SUCCEEDED`。

#### `t_patrol_job`

```text
job_id               VARCHAR(64) PK
batch_id             VARCHAR(64) FK
request_id           VARCHAR(191)
operating_unit_id    VARCHAR(64)
shop_id              BIGINT
site_code            VARCHAR(32)
parent_asin          VARCHAR(32)
parent_seller_sku    VARCHAR(128) NULL
shop_account_ref     VARCHAR(128)
job_type             VARCHAR(32)
status               VARCHAR(24)
payload_json         JSON
result_ref           VARCHAR(64) NULL
retry_count          INT
max_retry            INT
next_retry_at        DATETIME(6) NULL
locked_by            VARCHAR(128) NULL
locked_at            DATETIME(6) NULL
last_error_code      VARCHAR(64) NULL
last_error_message   TEXT NULL
created_at           DATETIME(6)
updated_at           DATETIME(6)
UNIQUE(batch_id, operating_unit_id)
INDEX(status, next_retry_at, created_at)
INDEX(operating_unit_id, created_at)
```

Worker 使用：

```sql
SELECT job_id
FROM t_patrol_job
WHERE status = 'PENDING'
  AND (next_retry_at IS NULL OR next_retry_at <= UTC_TIMESTAMP(6))
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

领取和改成 `RUNNING` 必须在同一事务中完成。

#### `t_patrol_run`

保存 `InspectionRun` 的完整运行状态、触发原因、规则版本、快照引用、信号统计、开始/结束时间及错误。Run 状态收敛为：

```text
RECEIVED
COLLECTING_FACTS
VALIDATING_FACTS
ANALYZING_ANOMALIES
RECONCILING_SIGNALS
PERSISTING
COMPLETED
COMPLETED_WITH_GAPS
BLOCKED
FAILED
```

删除 `EVALUATING_MODE/EVALUATING_ADVERTISING/BUILDING_PROPOSAL`。

#### `t_patrol_fact_snapshot`

保存：

- `snapshot_id`、`run_id`、`operating_unit_id`；
- `as_of`、`window_start`、`window_end`；
- `content_hash`、`quality_status`、`completeness_score`；
- `source_refs_json`、`data_gaps_json`、`normalized_summary_json`；
- `raw_reference_json` 保存对应 `t_patrol_raw_fact.raw_fact_id`，可直接定位原始证据。

金额使用 `DECIMAL(18,4)`，比率使用 `DECIMAL(10,6)`；禁止用二进制浮点承担持久化金额。

#### `t_patrol_raw_fact`

每个 Run、每个 MCP 事实工具保存一条不可变采集记录：

- 经营单元、业务日期、事实键、MCP Tool 和采集时间；
- 完整查询参数、参数哈希；
- MCP 完整响应封套、抽取后的数据数组、内容哈希和行数；
- 核心事实标记、耗时、警告、成功或失败状态；
- 失败调用的错误码和错误信息；
- 所属快照以及显式复用时的原始事实来源。

成功响应必须保存完整 MCP 原文，不能只保存摘要或哈希。默认巡检仍实时调用 MCP；只有
人工请求显式指定来源 Run 且经营单元一致、事实集合完整、内容哈希验证通过时，才允许
复用历史原始事实。复用产生新的 Raw Fact 记录并保留来源引用，不覆盖旧记录。

#### `t_patrol_signal`

```text
signal_id                VARCHAR(64) PK
dedup_key                CHAR(64) UNIQUE
operating_unit_id        VARCHAR(64)
issue_code               VARCHAR(64)
child_scope_key          VARCHAR(191) NOT NULL DEFAULT ''
signal_type              VARCHAR(32)
severity                 VARCHAR(24)
signal_state             VARCHAR(32)
action_timing_status     VARCHAR(32)
economic_currency        VARCHAR(16) NULL
economic_amount          DECIMAL(18,4) NULL
economic_window          VARCHAR(32)
constraint_flags_json    JSON
execution_readiness      VARCHAR(32)
diagnosis_json           JSON
handoff_json             JSON
first_detected_at        DATETIME(6)
last_detected_at         DATETIME(6)
last_scan_run_id         VARCHAR(64)
recurrence_count         INT
consecutive_miss_count   INT
next_inspection_at       DATETIME(6) NULL
valid_until              DATETIME(6) NULL
version                  BIGINT
created_at               DATETIME(6)
updated_at               DATETIME(6)
UNIQUE(operating_unit_id, issue_code, child_scope_key)
INDEX(signal_state, next_inspection_at)
```

MySQL 唯一索引允许多个 `NULL`，因此子体范围必须先规范化成非空 `child_scope_key`，链接级使用空字符串，不能直接用可空 `child_asin` 做唯一约束。

#### `t_patrol_signal_occurrence`

每轮保存 `DETECTED/MISSED/RECOVERED/RECURRED/DATA_GAP` 之一及当轮证据、严重度和快照引用。当前信号表保存最新状态，Occurrence 保存不可变历史。

#### `t_patrol_delivery_outbox`

Outbox 与 Run/Signal 在同一事务写入，投递器只重发原始 payload：

```text
outbox_id
aggregate_type
aggregate_id
aggregate_version
payload_hash
payload_json
status
retry_count
max_retry
next_retry_at
locked_by
locked_at
last_error
created_at
published_at
```

发送成功必须有接收方业务确认；仅 HTTP 连接成功不能视为交付成功。

## 8. 数据访问层改造

使用 SQLAlchemy 2 Core + PyMySQL + Alembic：

- SQLAlchemy 统一连接、事务、占位符和结果对象；
- PyMySQL 作为当前 MySQL 驱动；
- Alembic 管理所有 DDL，不允许应用启动时自动改生产表；
- Repository 隔离业务逻辑和数据库。

新增接口：

```text
PatrolBatchRepository
PatrolJobRepository
PatrolRunRepository
PatrolSignalRepository
FactSnapshotRepository
DeliveryOutboxRepository
IdempotencyRepository
SchedulerLockRepository
```

禁止在新代码中新增 `sqlite3.connect()`。现有 SQLite 访问只允许留在 `legacy/` 或兼容适配器中。

## 9. 文件级改造清单

### 9.1 新增

```text
alembic.ini
alembic/env.py
alembic/versions/0001_patrol_runtime.py
integrations/database.py
integrations/repositories/*.py
integrations/scheduler.py
integrations/delivery_worker.py
inspector/patrol_orchestrator.py
inspector/signal_reconciler.py
core/inspection_run.py
clients/result_sink.py
scripts/migrate_patrol_history.py
scripts/verify_mysql_cutover.py
```

### 9.2 修改

| 文件 | 修改 |
|---|---|
| `config/settings.yaml` | 增加 database/scheduler/delivery/features |
| `.env.example` | 增加 MySQL 和结论接收方环境变量名 |
| `requirements.txt` | 增加 SQLAlchemy、Alembic，锁定版本 |
| `core/enums.py` | 收敛 Run 状态；触发类型保留兼容 |
| `core/contracts.py` | 补正式 `InspectionRun`，输出收敛为巡检合同 |
| `web/backend/deps.py` | 装配 MySQL Repository 和新编排器 |
| `integrations/worker.py` | 使用 MySQL 队列，只处理巡检任务 |
| `web/backend/routers/patrol_v1.py` | 人工补跑和任务查询切 MySQL |
| `scripts/run_daily.sh` | 改为调用 Scheduler，不再 POST 旧批量线程接口 |
| `web/backend/main.py` | 不自动建表、不触发业务任务 |

### 9.3 从主链路断开

```text
clients/mode_agent_client.py
clients/advertising_agent_client.py
inspector/analyzer.py
inspector/proposal_builder.py
inspector/package_builder.py
inspector/review_orchestrator.py
web/backend/routers/review_v1.py
```

第一阶段不物理删除，避免影响旧页面和历史数据；通过组合根和 Feature Flag 断开。

## 10. SQLite 迁移和退役

### 10.1 不做整库机械复制

旧 SQLite 混合了事实缓存、旧打分、页面任务、人员权限和巡检生命周期。全部复制会把已退出职责一起带入新库。

分类处理：

| 数据 | 处理 |
|---|---|
| 有效异常和生命周期 | 迁移到 `t_patrol_signal` |
| 事件状态日志 | 迁移到 `t_patrol_signal_occurrence` |
| 巡检批次和结果 | 迁移为历史 Run，保留原始 JSON |
| 旧任务/投递运行态 | 仅迁移仍为活动状态且可确认的记录 |
| MCP 可重新取得的事实缓存 | 不迁移 |
| 旧综合分、P0/P1/P2 | 只在历史 JSON 中保留，不进入新字段 |
| 页面审批、执行和人员任务 | 不进入巡检私有库 |

### 10.2 切换步骤

1. 获取部署环境 `healthcheck.db` 和 `patrol_runtime.db`；
2. 冻结写入并生成 SHA-256 备份；
3. 运行只读盘点，输出表数、行数和异常状态分布；
4. 在测试库执行 Alembic 建表；
5. 预演迁移，校验主键、身份、活动信号和复发次数；
6. 新任务只写 MySQL，旧 SQLite 暂时只读；
7. 双读比对最新结果和活动信号；
8. MySQL 主链路稳定后关闭旧 `/api/batch/inspect`；
9. MCP 覆盖后停止旧事实同步和 SQLite 查询；
10. SQLite 文件归档，不删除原始备份。

最终目标是彻底去除运行时 SQLite；实施期允许旧页面短期只读兼容。

## 11. 对外结论输出

标准业务内容：

```text
InspectionRun
InspectionSignal[]
HandoffDirective[]
producer_versions
generated_at
```

不再输出：

```text
ModeDecisionResult
AdvertisingProposal
PatrolAnalysis
DecisionProposal
ActionOrder
```

HTTP 或消息投递协议尚未冻结，因此由 `ResultSinkPort` 隔离。接收方需要提供：

- 接口地址或 Topic；
- 鉴权方式；
- 请求和响应 Schema；
- 幂等键规则；
- 成功确认语义；
- 最大包大小和批量限制；
- 超时、限流和错误码；
- 重试和死信处理要求。

## 12. 分阶段实施

### 实施前质量门

正式编码前先恢复可信基线：

1. 修复 `python -m scripts.export_contracts --check` 当前报告的导出物漂移；
2. 使用项目支持的 Python 版本重建隔离环境，解决当前 Python 3.13 下测试进程异常退出；
3. 固化改造前的 R2/R3/R7、信号合同、幂等和旧规则回归结果；
4. 将 MySQL 集成测试与纯离线单元测试分组，测试库写入只能使用事务回滚或专用测试表；
5. 在质量基线恢复前不引用历史文档中的“193 例全绿”作为本次验收结论。

### P0：合同和数据库底座

1. 补 `InspectionRun` Schema；
2. 冻结巡检结论最小输出；
3. 建 SQLAlchemy/Alembic；
4. 创建 `t_patrol_*` 表；
5. 实现 Repository 和 MySQL 集成测试；
6. 建立独立、最小权限数据库账号。

### P1：自主调度和任务运行

1. 实现 Scheduler 租约；
2. 实现每日 02:00 和 07:00 幂等批次；
3. 实现到期复扫；
4. Worker 切 MySQL `SKIP LOCKED`；
5. 人工补跑接口切 MySQL；
6. 实现批次真实聚合状态。

### P2：巡检主链路收敛

1. 新建精简编排器；
2. 复用事实、质量、R2/R3 和信号生成；
3. 实现信号对账和恢复；
4. 断开模式、广告、建议和审批链路；
5. Run、Snapshot、Signal、Outbox 事务落库。

### P3：MCP Adapter

1. 接入经营单元清单工具；
2. 接入事实工具；
3. 固化字段映射、分页、时效和错误分类；
4. 对齐数据不足降级规则；
5. 用真实样本做规则回归。

### P4：结论投递和迁移切换

1. 实现 Result Sink；
2. 实现 Outbox 投递、重试和死信告警；
3. 迁移有效历史；
4. 双读比对；
5. 切换 MySQL 主链路；
6. 退役 SQLite 和旧批量线程。

## 13. 验收标准

### 13.1 调度

- 02:00 和 07:00 只产生一个每日业务批次；
- 多 Scheduler 实例不会重复建批次；
- 经营单元分页中断后可以继续；
- 到期复扫不会与日扫产生同单元并行任务；
- 人工重复提交符合幂等语义。

### 13.2 任务和数据库

- 多 Worker 不会领取同一任务；
- Worker 崩溃后任务可回收；
- 任务状态真实区分成功、部分成功和失败；
- Run、信号和 Outbox 原子提交；
- MySQL 严格模式下所有 DDL、查询和迁移通过；
- 新主链路没有 SQLite 写入。

### 13.3 巡检业务

- R2/R3 现有回归用例继续通过；
- 数据不足不生成虚假严重度或金额；
- 同一问题持续命中不创建重复活动信号；
- 恢复和复发可被准确识别；
- 子 ASIN 信号不会覆盖父级结论；
- 每个信号可追溯到事实快照和规则版本；
- 例行巡检不调用 LLM。

### 13.4 对外投递

- 接收方不可用时不丢结论；
- 重试发送的是同一原始 payload；
- 相同聚合版本不会被重复消费；
- 达到最大重试后进入 DEAD 并告警；
- 巡检不直接写 `t_ops_*`。

## 14. 外部待提供

| 事项 | 提供方 | 阻塞内容 |
|---|---|---|
| 经营单元清单 MCP 工具 | MCP/数据团队 | 全量调度 |
| 经营事实 MCP 工具和字段说明 | MCP/数据团队 | 正式事实采集 |
| 巡检结论接收合同 | 中控/下游 | Result Sink 联调 |
| 测试库建表授权确认 | 数据库负责人 | Alembic 首次建表 |
| 生产 MySQL 地址和最小权限账号 | 运维/DBA | 生产部署 |
| 部署环境 SQLite 文件 | 当前系统维护方 | 历史迁移 |

## 15. 当前测试库使用约束

当前测试库可以作为联调目标，但实施时必须遵守：

1. 新表统一使用 `t_patrol_*`；
2. 不修改现有 `t_ops_*`；
3. DDL 通过 Alembic 版本执行；
4. 首次建表前导出 Schema 备份；
5. 数据库密码只通过环境变量提供；
6. 正式环境换成巡检独立最小权限账号。

本方案阶段只完成设计和只读核验，尚未在测试库创建任何表。
