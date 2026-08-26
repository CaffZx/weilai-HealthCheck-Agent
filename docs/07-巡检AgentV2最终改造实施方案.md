# 巡检 Agent V2 最终改造实施方案

> 2026-08-04 数据库决策补充：V2 唯一运行数据库为 MySQL，SQLite 已完全弃用；本文涉及
> SQLite 只读观察、备份或迁移的过渡步骤均作废，不得作为当前操作依据。

> 状态：最终实施基线 v1.0  
> 日期：2026-07-31  
> 适用项目：`weilai-HealthCheck-Agent-v2.0`  
> 关联细化方案：`docs/06-巡检自主调度与MySQL改造方案.md`

## 1. 最终结论

巡检 Agent 的最终定位是：

> **自主触发、按经营单元获取事实、用确定性规则识别异常、维护信号生命周期，并把可追溯的巡检结论可靠交给中控。**

本项目不是经营决策 Agent，也不是执行 Agent。它不决定经营模式，不生成具体调价、补货、Listing 或广告执行指令，不负责审批和执行。

最终主流程为：

```text
巡检自主触发
→ 获取有效经营单元
→ 通过 MCP 获取经营事实
→ 校验并冻结事实快照
→ 执行现有 R2/R3 确定性规则
→ 生成本轮命中结果
→ 与历史活动信号对账
→ 识别首次、持续、恢复、复发和数据缺口
→ 事务保存 InspectionRun、InspectionSignal 和 Outbox
→ 向中控投递巡检结论
→ 接收中控处理反馈
→ 到期后由巡检自主复扫
→ 用新事实确认是否真正恢复
```

### 1.1 本期不重做的内容

- 不重构 R2 异常检测；
- 不重构 R3 严重度判定；
- 不扩充现有异常规则数量；
- 不改变“数据不足不猜测”的原则；
- 不做复杂外部事件驱动巡检；
- 不实现具体 MCP 工具业务方案，等后续方案冻结后接入 Adapter。

### 1.2 本期必须改的内容

- 从外部主触发改为巡检自主调度；
- 运行库从 SQLite 改为 MySQL；
- 主粒度统一为“店铺 × 站点 × 父 ASIN”；
- 接通信号状态机、恢复、复发和到期复扫；
- 输出统一为 `InspectionRun + InspectionSignal v2 + 必要的 HandoffDirective v2`；
- 删除综合分、P0/P1/P2 和 `operating_priority` 的新链路依赖；
- 断开模式判断、广告建议、经营建议、审批和结果复盘职责；
- 例行巡检零 LLM，人工复杂诊断最多一次；
- 增加可靠投递、回传事件幂等接收和全链路审计。

## 2. 依据和冲突处理

### 2.1 三类依据

| 类型 | 本方案采用的内容 |
|---|---|
| 闭环文档硬要求 | 统一经营单元、`InspectionSignal v2`、生命周期、恢复复发、四个确定性队列字段、职责不越界、例行零 LLM |
| 已确认的项目决策 | 巡检由我们自己触发；我们只把自产结论交给别人；事实通过本项目 MCP 包装层获取；运行态迁移 MySQL |
| 工程实现建议 | Scheduler 租约、MySQL 任务队列、事务 Outbox、回传 Inbox、Repository、Alembic、灰度和回滚方案 |

### 2.2 文档冲突的取值

早期《业务巡检Agent修正设计》保留了 `operating_priority` 和 R4；更新的外部改造说明、`InspectionSignal v2` 合同及 v1.3 直接切换规则明确删除综合分和 P0/P1/P2。

本方案采用更新口径：

- `severity` 保留，表示异常事实严重度；
- `signal_state` 保留，表示信号生命周期；
- `operating_priority`、R4 综合分和 P0/P1/P2 退出新主链路；
- 排序改用 `action_timing_status`、`economic_exposure`、`constraint_flags`、`execution_readiness` 四个确定性字段。

## 3. 项目定位和上下游

```text
经营单元主数据 / MCP 事实能力 / 历史回执
                    │
                    ▼
        ┌────────────────────────┐
        │      巡检 Agent V2      │
        │ 调度、事实、规则、生命周期 │
        │ 运行审计、可靠结论投递     │
        └────────────────────────┘
                    │
         InspectionRun / Signal
                    ▼
                 中控系统
       建任务、复核、审批、执行、回执
                    │
            处理反馈 / ActionReceipt 引用
                    ▼
        巡检记录反馈并自主安排复扫
```

### 3.1 上游提供什么

- 有效经营单元清单和身份绑定；
- 销售、流量、转化、库存、价格促销、Listing、广告、售后等事实；
- 每份事实的来源、截止时间、版本和质量信息；
- 可选的已批准经营计划、初始化状态、市场和成本证据；
- 中控处理反馈及真实执行回执引用。

### 3.2 巡检负责什么

- 自主调度每日全量巡检、到期复扫和人工补跑；
- 校验经营单元身份，不跨店、跨站点拼接；
- 获取、归一化、质检和冻结事实；
- 执行 R2/R3，生成异常和数据缺口；
- 维护首次发现、持续、恢复、复发和中断；
- 生成四个确定性队列字段和责任能力交接；
- 保存私有运行态，保证幂等、可恢复和可审计；
- 可靠投递巡检结论并接收处理反馈；
- 用后续复扫事实确认恢复，不能用“别人已处理”代替恢复。

### 3.3 下游负责什么

- 接收、校验、幂等保存巡检信号；
- 根据巡检信号生成任务或触发经营复核；
- 负责人分配、审批、执行和回执；
- 向巡检回传处理状态及回执引用；
- 展示经营闭环状态和业务任务；
- 不要求巡检直接写 `t_ops_*` 核心表。

### 3.4 明确不属于巡检的功能

- 经营模式判断和变更；
- 具体价格、库存、Listing、广告动作方案；
- `DecisionProposal`、`ActionOrder` 和审批；
- 执行动作和验证执行网关结果；
- 把 HTTP 提交成功当成经营问题解决；
- 中控业务任务的生命周期和页面。

## 4. 现有项目复用判断

复用度按“目标能力”评估，不按代码行数评估。当前约 **60% 的业务能力可以直接或经适配复用**，但自主调度、MySQL 运行态、生命周期对账和正式投递尚未完成，因此不能把现有代码量等同于上线完成度。

### 4.1 可以直接复用

| 能力 | 现有位置 | 处理 |
|---|---|---|
| 经营单元身份归一化 | `core/operating_unit.py` | 保留三元组和 ID 派生算法 |
| `InspectionSignal v2` 合同 | `core/contracts.py`、`core/enums.py` | 保留，补 `InspectionRun` 正式合同 |
| 事实采集编排骨架 | `facts/collector.py` | 保留端口思路，替换具体 MCP Adapter |
| 事实归一化和质量 | `facts/normalizer.py`、`facts/quality.py` | 保留，按新 MCP 字段补映射 |
| 不可变事实快照 | `facts/snapshot.py` | 保留内容哈希、来源引用和数据缺口，关联完整 Raw Fact |
| R2/R3 规则适配 | `inspector/legacy_rule_adapter.py` | 原业务规则不改 |
| R2/R3/R7 配置 | `inspector/rules/` | 保留并记录版本 |
| 信号构造主体 | `inspector/signal_builder.py` | 保留四字段、证据、诊断和交接构造 |
| 人工巡检接口形式 | `web/backend/routers/patrol_v1.py` | 保留请求语义，底层切 MySQL |
| 错误分类和契约测试 | `core/errors.py`、`tests/contract/` | 保留并扩展 |

### 4.2 可以复用思路但必须重写存储实现

| 能力 | 当前问题 | 改造 |
|---|---|---|
| 任务队列 | SQLite、单机领取 | 改成 MySQL `SKIP LOCKED` 和租约回收 |
| 幂等记录 | SQLite | 改成 MySQL 唯一约束和请求哈希 |
| 待投递 | 只是本地 pending package | 改成与信号同事务的 Outbox |
| 信号去重 | 只处理命中，缺完整恢复对账 | 改成当前态 + occurrence 历史 |
| Worker | 依赖旧编排器和 SQLite | 改成精简巡检 Worker |
| 每日监控 | 指标计算可用，不是完整 Scheduler | 保留计算，新增自主 Scheduler |

### 4.3 从新主链路断开

| 模块 | 原因 |
|---|---|
| `clients/mode_agent_client.py` | 巡检不判断经营模式 |
| `clients/advertising_agent_client.py` | 本期广告能力冻结，巡检只交接 |
| `inspector/analyzer.py` | 当前含经营建议聚合和跨域决策职责 |
| `inspector/proposal_builder.py` | 巡检不生成经营建议和审批对象 |
| `inspector/package_builder.py` | 旧包混合模式、广告和建议，需换成巡检结论包 |
| `inspector/review_orchestrator.py` | 经营结果复盘属于中控/决策闭环 |
| R4 综合分及 P 级 | 新合同明确退出 |
| 旧 SQLite 页面任务和审批 | 不属于巡检私有运行态 |

## 5. 目标功能清单

### 5.1 调度与任务

- 每日 `02:00` 全量巡检；
- `07:00` 幂等兜底，不重复建批次；
- 每 15 分钟扫描一次到期复扫；
- 支持人工指定经营单元补跑；
- 多实例 Scheduler 只允许一个持有租约；
- 单经营单元同一时刻只允许一个活动任务；
- Worker 异常退出后任务可回收；
- 批次真实区分完成、部分完成和失败。

### 5.2 事实与证据

- 分页获取全部有效经营单元；
- 校验中控经营单元业务键 `shop_id + parent_asin + parent_seller_sku`；
- `site_code` 作为业务属性和 MCP 取数参数保留，不参与唯一性；
- 通过 MCP 获取规则所需事实；
- 保存来源、请求哈希、内容哈希、数据截止时间和质量；
- 关键事实缺失时明确输出数据缺口；
- 不用 `0`、默认站点或估算值掩盖缺失；
- 同一轮规则只读取冻结后的同一事实版本。

### 5.3 规则与信号

- 复用现有 R2 异常识别和 R3 严重度；
- 子 ASIN 只进入 `child_scope`；
- 生成异常、机会、恢复、复发、数据质量和到期观察信号；
- 生成四个确定性队列字段；
- 记录证据、诊断、规则版本、首次和最近发现时间；
- 例行流程不调用 LLM；
- 人工复杂诊断最多调用一次，且不能覆盖确定性字段。

### 5.4 生命周期

- 识别首次发现和持续命中；
- 已关闭问题再次命中时识别为复发并累计次数；
- 接收“处理中、已处理、待观察、忽略、误报”等下游反馈；
- “已处理”只能进入 `AWAITING_RESCAN`；
- 完整事实下连续两次未命中才进入 `RESOLVED`；
- 数据缺失时不增加未命中次数；
- 每次状态变化保存不可变 occurrence 和来源事件；
- 状态非法跳转必须拒绝并记录。

### 5.5 投递和运维

- Run、信号、Occurrence 和 Outbox 原子提交；
- 只重投原始 payload，不重新生成结论；
- 接收方业务 ACK 后才标记投递成功；
- 回传事件通过 Inbox 幂等消费；
- 超过最大重试进入 DEAD 并告警；
- 提供批次、任务、运行、投递和依赖健康指标；
- 敏感配置只通过环境变量或密钥中心注入。

## 6. 完整业务流程

### 6.1 触发

本期只有三种正式触发：

| 触发类型 | 发起方 | 范围 | 用途 |
|---|---|---|---|
| `DAILY_SCHEDULE` | 巡检 Scheduler | 全部有效经营单元 | 每日主巡检 |
| `OBSERVATION_DUE` | 巡检 Scheduler | 到期信号涉及的单元 | 处理后复扫和观察到期 |
| `MANUAL` | 巡检 API/管理端 | 指定单元或范围 | 补跑、复现、人工检查 |

外部中控不负责日常触发巡检。它回传“已处理”后，巡检记录 `next_inspection_at`，再由自己的 Scheduler 到期触发复扫。

`DATA_REPAIRED`、`CHANGE_TRIGGERED`、`PERFORMANCE_TRIGGERED` 等类型只保留枚举，不在本期实现。

### 6.2 每日全量流程

```text
02:00 获取 Scheduler 租约
→ 调 MCP 获取有效经营单元清单
→ 过滤 EXITED / ARCHIVED / 无效单元
→ 生成 scope_hash 和每日批次幂等键
→ 每 50 个单元分片入队
→ Worker 逐单元执行巡检
→ 汇总批次状态
→ 运行本批次 reconcile
→ 生成批次统计和告警

07:00 再次尝试相同业务日批次
→ 唯一约束命中则返回原批次
→ 不重复执行
```

每日批次幂等键：

```text
DAILY_SCHEDULE:{business_date}:{scope_hash}:{rule_bundle_version}
```

### 6.3 单经营单元巡检

```text
领取 Job
→ 校验经营单元身份
→ 创建 InspectionRun(RECEIVED)
→ COLLECTING_FACTS：调用 MCP
→ VALIDATING_FACTS：归一化、时效和完整性校验
→ 冻结 FactSnapshot 和 content_hash
→ ANALYZING_ANOMALIES：运行 R2/R3
→ 构造本轮候选 InspectionSignal
→ RECONCILING_SIGNALS：与历史信号对账
→ PERSISTING：同事务保存 Run、Signal、Occurrence、Outbox
→ COMPLETED / COMPLETED_WITH_GAPS / BLOCKED / FAILED
```

### 6.4 信号对账规则

| 历史情况 | 本轮情况 | 结果 |
|---|---|---|
| 无历史信号 | 命中 | 创建 `NEW`，`recurrence_count=0` |
| 活动信号 | 再次命中 | 更新最近发现时间，未命中次数归零，保存 `DETECTED` |
| `RESOLVED` 信号 | 再次命中 | 转为复发，`recurrence_count + 1`，保存 `RECURRED` |
| 活动信号 | 未命中且事实完整 | `consecutive_miss_count + 1` |
| 活动信号 | 连续第二次未命中 | 转 `RESOLVED`，保存 `RECOVERED` |
| 活动信号 | 本轮关键数据缺失 | 状态和未命中次数不变，保存 `DATA_GAP` |
| 单元退出或归档 | 不再扫描 | 转 `INTERRUPTED`，不能伪装为恢复 |

信号业务唯一键：

```text
shop_id + parent_asin + parent_seller_sku + issue_code + normalized_child_scope
```

### 6.5 结论投递

巡检对外投递的是自产结论，不是任务和动作：

```text
InspectionResultEnvelope
├── request_id / trace_id
├── InspectionRun
├── InspectionSignal[]
├── HandoffDirective[]
├── producer_versions
└── generated_at
```

接收方必须返回业务 ACK，至少包含：

```text
event_id
accepted
accepted_object_ids
rejected_objects
duplicate
received_at
```

HTTP `2xx` 但没有可验证业务 ACK，只能视为传输成功，不能把 Outbox 标记为业务投递完成。

### 6.6 中控处理回传

中控发布/回传事件的用途不是替我们判断异常，而是告诉巡检“下游对这个信号做了什么”，使生命周期和复扫时间可以衔接。

建议回传事件：

| 事件 | 巡检侧处理 | 是否直接关闭 |
|---|---|---|
| `HANDLING_STARTED` | 信号转 `IN_PROGRESS` | 否 |
| `HANDLED` | 信号转 `AWAITING_RESCAN`，记录计划复扫时间 | 否 |
| `OBSERVE_REQUESTED` | 信号转 `OBSERVING`，记录观察窗口 | 否 |
| `IGNORED` | 信号转 `IGNORED`，要求原因码 | 是，业务忽略而非事实恢复 |
| `FALSE_POSITIVE` | 信号转 `FALSE_POSITIVE`，进入规则反馈池 | 是，误报而非事实恢复 |
| `ACTION_RECEIPT_AVAILABLE` | 保存回执引用，供下一次巡检对比 | 否 |
| `UNIT_ARCHIVED` | 信号转 `INTERRUPTED` | 是，周期中断而非事实恢复 |

所有回传必须带 `event_id`、`signal_id`、`signal_version`、发生时间、来源、原因或回执引用。重复 `event_id` 返回原处理结果；旧版本或非法状态跳转拒绝消费。

### 6.7 到期复扫

```text
Scheduler 每 15 分钟查询 next_inspection_at 已到期信号
→ 按 operating_unit_id 合并
→ 排除已有活动 Job 的单元
→ 创建 OBSERVATION_DUE 批次和 Job
→ 重新获取事实并运行完整规则
→ reconcile 判断持续、恢复或数据缺口
→ 再次投递新版本信号
```

复扫不是读取旧快照重算，必须重新取得事实并生成新快照。

## 7. 对外接口和 MCP 边界

### 7.1 巡检自身接口

| 接口 | 用途 |
|---|---|
| `POST /api/v1/patrol/runs` | 人工创建指定范围巡检，要求 `X-Request-Id` |
| `GET /api/v1/patrol/runs/{run_id}` | 查询单次运行和统计 |
| `GET /api/v1/patrol/batches/{batch_id}` | 查询批次真实聚合状态 |
| `POST /internal/v1/patrol/feedback-events` | 幂等接收中控处理反馈 |
| `GET /api/v1/patrol/signals/{signal_id}` | 查询当前信号及历史 |
| `GET /api/v1/patrol/health` | 依赖、队列、调度和投递健康检查 |

人工创建巡检接口是补跑能力，不是中控日常逐单元调用入口。

### 7.2 MCP 稳定端口

具体工具名和参数以后续 MCP 方案为准，当前只冻结端口：

```python
class OperatingUnitProviderPort(Protocol):
    async def list_active_units(self, *, cursor: str | None, limit: int) -> UnitPage: ...

class FactProviderPort(Protocol):
    async def collect(self, unit: OperatingUnitBinding, *, as_of: date) -> RawFactBundle: ...
```

MCP 能力至少覆盖：

- 经营单元清单和取数绑定；
- 销售、流量和转化；
- 库存、库龄和补货事实；
- 价格和促销；
- Listing、图片、类目和前台可售状态；
- 广告事实；
- 评分、评论、退款和售后；
- 每类事实的版本、时效、来源和错误类别。

成本、市场、Approved Plan 和执行回执不是所有异常规则的硬前置。缺失时相应字段必须为 `UNASSESSED` 或 `BLOCKED_DEPENDENCY`，不得伪造结果。

### 7.3 结论接收端口

```python
class ResultSinkPort(Protocol):
    async def publish(self, envelope: InspectionResultEnvelope) -> DeliveryAck: ...
```

HTTP、消息队列或 MCP 只是 Adapter 选择，不改变巡检领域流程。

## 8. MySQL 最终设计

### 8.1 数据库原则

- 新表统一使用 `t_patrol_*`；
- 不修改、不直接写中控 `t_ops_*`；
- 使用 SQLAlchemy 2 Core + PyMySQL；
- 使用 Alembic 管理 DDL；
- 应用启动时不自动建生产表；
- 时间统一存 UTC，展示层转 `Asia/Shanghai`；
- 金额用 `DECIMAL(18,4)`，比例用 `DECIMAL(10,6)`；
- Python 合同和计算层使用 `Decimal`，统一采用 `ROUND_HALF_UP`；对外 JSON 使用十进制定点字符串或经合同确认的四位小数数值，禁止从二进制 `float` 直接落库；
- 密码只通过环境变量或密钥中心注入；
- 生产使用最小权限独立账号。

### 8.2 表清单

| 表 | 作用 | 关键约束 |
|---|---|---|
| `t_patrol_batch` | 巡检批次 | `idempotency_key` 唯一 |
| `t_patrol_job` | 单元任务队列 | `(batch_id, operating_unit_id)` 唯一 |
| `t_patrol_run` | 单元运行记录 | `run_id`、`request_id` 唯一 |
| `t_patrol_fact_snapshot` | 事实快照摘要和引用 | `snapshot_id`、`content_hash` |
| `t_patrol_raw_fact` | MCP 原始请求、完整响应及复用血缘 | `(run_id, fact_key)` |
| `t_patrol_signal` | 信号当前态 | 经营单元 + 问题 + 子体范围唯一 |
| `t_patrol_signal_occurrence` | 每轮命中、未命中、恢复和复发历史 | `(signal_id, run_id, occurrence_type)` |
| `t_patrol_feedback_inbox` | 中控回传事件及消费结果 | `event_id` 唯一 |
| `t_patrol_delivery_outbox` | 结论可靠投递 | 对象 + 版本唯一 |
| `t_patrol_idempotency` | 写接口幂等 | actor + key + route 唯一 |
| `t_patrol_scheduler_lock` | Scheduler 租约 | `lock_name` 主键 |
| `t_patrol_schema_version` | 迁移审计 | `version` 主键 |

### 8.3 必须原子提交的内容

单元巡检成功时，以下数据必须在同一个数据库事务中完成：

```text
更新 InspectionRun
+ 写 FactSnapshot
+ 新增/更新 Signal
+ 写 SignalOccurrence
+ 写 DeliveryOutbox
```

任何一步失败全部回滚，Job 进入可重试或失败状态。不能出现“信号已保存但没有投递记录”或“已投递但本地没有信号版本”。

### 8.4 回传 Inbox 处理

```text
收到 feedback event
→ 按 event_id 插入 Inbox
→ 校验 signal_id 和 signal_version
→ 校验状态转换
→ 更新 Signal / next_inspection_at
→ 标记 Inbox APPLIED
```

Inbox 与信号状态更新必须同事务。处理失败保留错误，不反复改变状态。

## 9. 代码改造清单

### 9.1 新增

```text
alembic.ini
alembic/env.py
alembic/versions/0001_patrol_runtime.py
core/inspection_run.py
core/result_envelope.py
integrations/database.py
integrations/repositories/*.py
integrations/scheduler.py
integrations/delivery_worker.py
integrations/feedback_consumer.py
inspector/patrol_orchestrator.py
inspector/signal_reconciler.py
clients/result_sink.py
web/backend/routers/feedback_v1.py
scripts/migrate_patrol_history.py
scripts/verify_mysql_cutover.py
```

### 9.2 修改

| 文件 | 修改内容 |
|---|---|
| `config/settings.yaml` | 增加 database、scheduler、delivery、feedback 和 feature flags |
| `.env.example` | 只增加变量名，不写真实密码或 Token |
| `core/contracts.py` | 补 `InspectionRun` 和纯巡检结果封套 |
| `core/contracts.py`、`inspector/signal_builder.py` | 经济暴露从 `float` 收敛为 `Decimal`，统一四位小数和舍入规则 |
| `core/enums.py` | 收敛运行状态和触发类型，保留信号 10 态 |
| `inspector/signal_builder.py` | 去掉对 Mode Agent 结果的硬依赖 |
| `web/backend/deps.py` | 装配 MySQL Repository 和精简编排器 |
| `integrations/worker.py` | 使用 MySQL 队列和新编排器 |
| `web/backend/routers/patrol_v1.py` | 接口切 MySQL 并补批次查询 |
| `web/backend/main.py` | 不自动建表、不启动业务线程 |
| `scripts/run_daily.sh` | 调 Scheduler 命令，不调用旧批量接口 |
| `requirements.txt` / `pyproject.toml` | 增加并锁定 SQLAlchemy、Alembic、PyMySQL |

### 9.3 保留但断开

模式、广告、建议、旧包和复盘相关文件第一阶段不物理删除，通过组合根和 Feature Flag 断开，便于回退和历史页面只读。MySQL 主链路稳定后再单独清理。

## 10. 分阶段任务和工作安排

以下工期是单个熟悉项目的后端开发工程估算，不含外部接口等待和真实数据修复时间。

| 阶段 | 主要任务 | 预计工期 | 退出条件 |
|---|---|---:|---|
| S0 基线恢复 | 固定 Python 版本、修复合同导出漂移、冻结 R2/R3/R7 回归 | 2–3 人日 | 基线测试可重复运行 |
| S1 合同与 MySQL | `InspectionRun`、结果封套、Alembic、11 张表、Repository | 4–6 人日 | MySQL 集成测试通过 |
| S2 调度与生命周期 | 02:00/07:00、到期复扫、Worker、reconcile、状态机 | 5–7 人日 | 首次/持续/恢复/复发用例通过 |
| S3 主链路收敛 | 新编排器、断开越界模块、事务保存、例行零 LLM | 4–6 人日 | 单元巡检端到端通过 |
| S4 MCP 接入 | 单元清单、事实映射、分页、时效、错误和限流 | 4–7 人日 | 真实样本规则回归通过 |
| S5 投递与回传 | Result Sink、Outbox、ACK、Feedback Inbox、告警 | 4–6 人日 | 断网重投和回传幂等通过 |
| S6 迁移上线 | 历史盘点、试迁移、双读、灰度、切换和归档 | 3–5 人日 | MySQL 主链路稳定且可回滚 |

预计内部开发总量：**26–40 人日**。如果 MCP 和中控合同能提前冻结，S4 与 S2/S3 的后半段可以并行；否则外部等待会成为关键路径。

### 10.1 实施顺序

```text
先冻结合同和测试基线
→ 再建 MySQL 底座
→ 再做自主调度和生命周期
→ 再收敛主编排器
→ 接正式 MCP
→ 接中控投递和反馈
→ 迁移历史并灰度切换
```

不能先把旧 SQLite 整库搬到 MySQL，再判断哪些属于巡检；这样会把审批、人员任务、旧综合分和旧页面职责一起带入新主链路。

## 11. 我们自己要完成的事项

| 优先级 | 事项 | 交付物 |
|---|---|---|
| P0 | 修复可信测试基线 | 固定环境、合同一致性、R2/R3/R7 回归报告 |
| P0 | 冻结巡检领域合同 | `InspectionRun`、结果 Envelope、反馈 Event Schema |
| P0 | MySQL 运行底座 | Alembic、Repository、事务和 11 张私有表 |
| P0 | 金额精度统一 | Python `Decimal`、JSON 表达和 MySQL `DECIMAL` 一致性测试 |
| P0 | 精简巡检编排器 | 只含事实、规则、对账、保存和投递 |
| P0 | 生命周期 Reconciler | 首次、持续、恢复、复发和数据缺口 |
| P1 | 自主 Scheduler 和 Worker | 日扫、兜底、复扫、租约和任务恢复 |
| P1 | MCP Adapter | 按外部方案完成工具包装和字段映射 |
| P1 | Result Sink 和 Feedback Inbox | 可靠投递、ACK、回传幂等和状态转换 |
| P1 | SQLite 历史迁移 | 只迁移有效巡检信号和必要历史 |
| P1 | 监控和运维 | 指标、日志、告警、Runbook 和回滚脚本 |
| P2 | 清理旧链路 | 下线旧批量接口、SQLite 写入和越界模块 |

## 12. 外部需求和问题

这些不是本项目实现，但没有它们会影响完整上线。

| 外部需求 | 提供方 | 我们需要的内容 | 不提供的影响 |
|---|---|---|---|
| 经营单元清单 MCP | MCP/数据团队 | 完整三元组、取数绑定、有效状态、分页、版本 | 无法自主全量调度 |
| 经营事实 MCP | MCP/数据团队 | 字段、来源、时效、分页、错误码、限流、样例 | 无法替换 SQLite 事实来源 |
| 结果接收合同 | 中控团队 | 地址/Topic、鉴权、Schema、幂等键、业务 ACK | 只能本地保存，不能闭环交付 |
| 处理反馈合同 | 中控团队 | 反馈事件、状态映射、版本、回执引用、重试 | 无法知道已处理和安排准确复扫 |
| 初始化/批准计划状态 | 经营决策/中控 | 初始化是否完成、当前计划引用 | `execution_readiness` 只能阻断或未评估 |
| 测试库建表授权 | DBA | `t_patrol_*` DDL 审核和执行权限 | MySQL 集成无法开始 |
| 生产数据库 | DBA/运维 | 独立库或最小权限账号、备份、监控 | 无法生产部署 |
| 部署调度能力 | 运维 | Scheduler/Worker 部署、副本数、时区和告警 | 自主触发无法稳定运行 |
| 旧 SQLite 数据 | 当前系统维护方 | 生产文件、表说明、历史状态含义 | 无法可靠迁移活动信号 |
| 业务口径确认 | 业务 Owner | 第 13 节各项结论 | 恢复、范围和状态存在业务歧义 |

## 13. 业务 Owner 必须确认的口径及影响

| 需要确认的业务口径 | 当前建议默认值 | 影响 |
|---|---|---|
| 有效经营单元范围 | 排除 `EXITED/ARCHIVED/无效` | 决定每天巡检对象、覆盖率和资源量 |
| 恢复确认次数 | 连续 2 次完整巡检未命中 | 次数小会假恢复，次数大则关闭延迟 |
| 人工“完成”的含义 | 只到 `AWAITING_RESCAN` | 决定能否用人工动作直接关闭异常 |
| 到期复扫时间来源 | 优先外部观察计划，否则 R7 | 决定何时验证处理效果 |
| 数据缺失是否参与恢复 | 不参与、不计数 | 防止同步失败造成假恢复 |
| 子体允许下降的异常 | 断货、低库存、滞销、价格及子体特有异常 | 决定信号数量和父子体责任边界 |
| 严重度阈值 | 本期沿用 R3 | 决定 S0/S1/S2 分布和告警强度 |
| 经济暴露无法计算 | `UNASSESSED`，不填 0 | 决定队列排序和页面解释 |
| 忽略和误报是否终止活动信号 | 允许，但必须有原因码 | 决定规则反馈和审计语义 |
| 单元退出后的信号 | `INTERRUPTED`，不记恢复 | 决定历史统计是否真实 |
| 历史信号保留周期 | 建议长期保留摘要和 occurrence | 决定审计、复发识别和存储成本 |
| 人工复杂诊断权限 | 主动触发、一次、只解释 | 决定 LLM 成本和是否可能越权判断 |

业务 Owner 未确认前，可以按建议默认值开发，但不能把默认值称为最终业务规则；所有默认值必须版本化，并能通过配置和迁移调整。

## 14. 测试和验收

### 14.1 质量基线

- 使用项目支持的 Python 版本重建隔离环境；
- 修复合同导出物漂移；
- 不引用历史“193 项全绿”作为本次结论；
- R2/R3/R7 旧回归必须在改造前后使用同一输入对比；
- MySQL 测试使用专用表或事务回滚，不污染共享测试数据。

### 14.2 核心业务验收

- 同一父 ASIN 在两个站点生成两个不同经营单元；
- 子 ASIN 异常只出现在 `child_scope`；
- 同一问题重复命中不创建第二个活动信号；
- 已关闭问题再次命中正确累计复发；
- 完整事实连续两次未命中才恢复；
- 数据缺失不增加恢复计数；
- 人工 `HANDLED` 不直接关闭；
- 退出单元进入 `INTERRUPTED`；
- 例行巡检没有 LLM 调用；
- 新输出没有综合分、P 级和具体执行动作。

### 14.3 可靠性验收

- `02:00` 和 `07:00` 只生成一个业务日批次；
- 两个 Scheduler 不重复建批次；
- 多 Worker 不重复领取任务；
- Worker 崩溃后任务可回收；
- Run、Signal、Occurrence 和 Outbox 原子提交；
- 中控不可用时结论不丢失；
- 重试 payload 哈希不变化；
- 重复反馈事件只消费一次；
- 非法状态跳转被拒绝；
- 达到最大重试后进入 DEAD 并告警。

### 14.4 合同验收

- `InspectionSignal` 通过上游 v2 Schema；
- `OperatingUnitRef` 三元组和派生 ID 一致；
- 每条信号具有证据、诊断、规则版本和事实版本；
- 金额和比率精度在 Python、JSON 和 MySQL 间一致；
- 接收 ACK 和反馈 Event 有正式 Schema 及幂等测试。

## 15. 上线和回滚

### 15.1 上线步骤

1. 冻结旧运行库写入窗口并备份；
2. 审核并执行 Alembic 建表；
3. 先上线 MySQL Scheduler/Worker，但只跑影子批次；
4. 同经营单元对比旧结果和新结果；
5. 接入中控测试 Sink 和 Feedback；
6. 迁移活动信号及必要 occurrence；
7. 小范围店铺灰度；
8. 全量启用 MySQL 主链路；
9. 旧 SQLite 保持只读观察一个周期；
10. 停止旧批量接口和 SQLite 写入，归档不删除。

### 15.2 回滚条件

- 重复批次或重复活动信号超过阈值；
- 大量身份无法解析或发生跨站点串号；
- R2/R3 同输入结果出现无法解释的漂移；
- Outbox 持续积压且无法安全重投；
- 生命周期出现非法跳转或假恢复；
- MySQL 严格模式下存在数据截断或精度损失。

回滚只切换调度和读写 Feature Flag，不删除 MySQL 数据，不覆盖旧 SQLite 备份，不重放已被中控确认接收的不同版本 payload。

## 16. 最终交付清单

项目完成时必须同时交付：

- 最终合同：`InspectionRun`、结果 Envelope、反馈 Event；
- MySQL Alembic 迁移和表说明；
- 自主 Scheduler、Worker、Reconciler；
- MCP Adapter 和字段映射说明；
- Result Sink、Outbox、Feedback Inbox；
- R2/R3/R7 回归报告；
- 生命周期和幂等端到端测试；
- 历史迁移盘点、预演和校验报告；
- 监控面板、告警和运维 Runbook；
- 灰度上线和回滚记录；
- 外部接口联调确认单；
- 业务 Owner 口径确认单。

## 17. 当前可立即开始与阻塞项

### 可以立即开始

- S0 测试基线恢复；
- `InspectionRun` 和反馈事件合同设计；
- MySQL Alembic 与 Repository；
- Scheduler、任务队列和信号 Reconciler；
- 旧主链路职责拆分；
- 离线 Fake MCP、Fake Sink 和 Feedback 测试。

### 等外部输入后完成

- 正式 MCP 工具映射和真实数据回归；
- 中控结果接收联调；
- 中控处理反馈联调；
- 生产数据库和部署；
- 业务口径最终冻结；
- 生产 SQLite 历史迁移。

这意味着项目不需要等待所有外部团队才开工，但在 MCP、结果接收、反馈合同和业务口径未冻结前，不能宣布完整闭环上线。
