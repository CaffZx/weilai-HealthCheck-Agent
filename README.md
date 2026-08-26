# weilai-HealthCheck-Agent v2.0

Amazon 运营闭环中的纯巡检 Agent。当前唯一实施基线是
`docs/07-巡检AgentV2最终改造实施方案.md`。

## 职责边界

当前主链路：

```text
Scheduler / 人工 API
→ MySQL 任务队列
→ Worker 领取经营单元
→ MCP 采集事实并逐次归档原始请求与响应
→ 归一化并冻结事实快照
→ R2/R3 确定性规则识别异常
→ Signal 生命周期对账
→ InspectionRun、Signal、RawFact、Outbox 同事务保存
→ Result Sink 原包可靠投递
→ Feedback Inbox 接收处理反馈并安排复扫
```

巡检只产出有证据引用的异常结论和交接方向，不判断经营模式、不生成经营建议、
不审批、不执行，也不直接修改中控数据库。经营模式只通过经营模式 Agent 的只读 MCP
查询当前权威结果；该查询已在完整中控载荷冻结前启用，内部纯巡检 Envelope 不混入经营
模式。广告建议、旧复盘、SQLite 工作台和 LLM 建议链路已从源码中删除。

## 原始事实

每次 MCP 调用都会写入 MySQL `t_patrol_raw_fact`，包括：

- 巡检批次、任务、运行和经营单元身份；
- MCP Provider、Tool、完整请求参数和原始响应；
- 抽取后的业务数据、内容哈希、采集时间和错误信息；
- 显式复用时的来源运行和来源原始事实血缘。

人工巡检可传 `reuse_fact_run_id` 复用指定历史运行的事实。系统不会隐式使用“最近
一次”数据，避免把过期或错误身份的事实混入当前判断。

取数统一按完整自然日计算：传入的 `as_of` 是巡检运行日，所有日期型事实截止到运行日前
一天。默认 30 天窗口包含截止日在内共 30 个自然日；关键词排名因上游最多支持 15 天，
固定查询最近 14 个完整自然日。例如 2026-08-05 巡检时，默认窗口为
2026-07-06～2026-08-04，关键词排名窗口为 2026-07-22～2026-08-04。快照元数据中的
`inspection_date` 保存巡检运行日，`as_of/window_end` 保存事实截止日。

查询某次运行的事实归档：

```http
GET /api/v1/patrol/runs/{run_id}/facts
```

## 运行

环境要求：Python 3.12.11、MySQL 8、可访问的 MCP 服务。

```bash
uv venv --python 3.12.11 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
cp .env.example .env
./scripts/verify_s0.sh
```

启动 API：

```bash
./start.sh
```

启动独立调度器或 Worker：

```bash
python -m integrations.runtime_scheduler_main daily
python -m integrations.runtime_worker_main
python -m integrations.runtime_delivery_main
python -m integrations.runtime_control_center_delivery_main
```

`rollout.stage=INTERNAL_ONLY` 会同时阻止 Scheduler 和 Worker；只打开单个 Feature
Flag 不能绕过总闸门。数据库迁移必须由部署流程显式执行，API 启动不会建表或写业务数据。

## 配置

核心环境变量见 `.env.example`：

| 环境变量 | 用途 |
|---|---|
| `PATROL_DATABASE_URL` | SQLAlchemy 格式的 MySQL URL |
| `AZLISTING_GATEWAY` | 主 MCP 地址 |
| `MCP_API_KEY` | MCP 访问令牌 |
| `AZLISTING_PRINCIPAL_SCOPES_JSON` | 可选的负责人巡检范围映射 |
| `INSPECTION_RESULT_SINK_URL` | 纯巡检结果接收地址 |
| `INSPECTION_RESULT_SINK_TOKEN` | 结果投递令牌 |
| `INSPECTION_FEEDBACK_TOKEN` | Feedback API 令牌 |
| `CONTROL_CENTER_MCP_URL` | 中控 `submit_patrol_batch` MCP 地址；默认 `https://mcp-gateway.example.com/opsloop/mcp` |
| `CONTROL_CENTER_MCP_TOKEN` | 中控 MCP 投递令牌 |

当前正式启用 OpsLoop 中控投递，旧 Result Sink、调度器和反馈链路保持关闭：

```yaml
rollout:
  stage: FULL
  shadow_mode: false

feature_flags:
  scheduler_enabled: false
  result_delivery_enabled: false
  control_center_delivery_enabled: true
  feedback_enabled: false
```

部署环境必须提供 `CONTROL_CENTER_MCP_TOKEN`，并单独运行
`python -m integrations.runtime_control_center_delivery_main`。该 Worker 只投递数据库中已经进入
`PENDING` 状态的中控 Outbox；当前配置不会自动启动每日巡检调度。

## 目录结构

```text
clients/                     MCP 客户端、安全校验和机器合同
facts/                       采集、归一化、质量检查、快照与补充事实
inspector/
  engine/                    保留的 R2/R3 确定性规则引擎
  rules/                     R2/R3/R7 版本化规则
  legacy_rule_adapter.py     新事实快照到 R2/R3 的纯内存适配
  patrol_orchestrator.py     当前纯巡检编排器
  signal_builder.py          契约信号构造与确定性排序
  signal_reconciler.py       首次、持续、恢复和复发生命周期
integrations/
  database.py                MySQL Engine
  runtime_queue.py           MySQL 任务队列与租约
  runtime_worker.py          当前 Worker
  scheduler.py               调度与经营单元批次创建
  repositories/              MySQL Repository 与 13 张运行表
  result_delivery.py         Outbox 原包投递
  control_center_delivery.py 中控 MCP 完整载荷原包投递
  feedback.py                Inbox 幂等和 R7 复扫
web/backend/                 当前 API、组合根和旧 API 410 防护
alembic/                     MySQL schema 迁移，当前 revision 0005_patrol_review
contracts/                   共享 JSON Schema 与 MCP 机器合同
tests/                       合同、单元、集成及真实 MySQL 测试
docs/                        实施、部署、联调和运维文档
```

项目运行目录不再包含 SQLite 数据库。旧 `/api/tasks`、`/api/inspect` 等路径固定返回
`410 SQLITE_RETIRED`。

## 验证基线

当前测试收集共 488 项：

| 分层 | 数量 | 内容 |
|---|---:|---|
| 合同测试 | 66 | JSON Schema、枚举镜像、MCP 工具白名单 |
| 单元测试 | 371 | 事实、规则、Signal、调度、Worker、投递、反馈、查询、观测和复盘入口安全 |
| 集成测试 | 51 | HTTP、工作台、迁移、Repository、MySQL 主链路和事实复用 |
| 合计 | 488 | 其中 6 项真实 MySQL 测试无 URL 时跳过 |

常用验证：

```bash
ruff check .
pytest -q
python -m scripts.export_contracts --check
```

本地中控联调使用 `python -m scripts.local_integration`；完整本地闭环可使用
`python -m scripts.run_full_local_e2e`，环境准备和真实/Mock 边界见
`docs/24-本地中控联调运行手册.md`。

真实 MySQL 验证使用独立测试库 URL，不清理既有业务数据；验证脚本只删除自己创建的
测试记录。

## 关键规则

1. 缺失事实必须表达为数据缺口，不能填 0、999 或其它业务默认值。
2. 严重度与任务排序分离，不允许恢复“综合执行分”。
3. 例行巡检不调用 LLM，规则结果必须确定且可重放。
4. Result Sink 失败时重投 Outbox 原 payload，不能重新巡检生成另一份结果。
5. 下游“已处理”不是事实恢复，必须由后续完整复扫确认。
6. MCP 原始响应、归一化事实和结论之间必须能通过 run、raw fact 和 hash 追溯。

## 当前门禁

- MySQL schema、运行队列、事实归档和内部闭环已经实现并通过测试。
- `/ui` 已切换为纯 MySQL V2 只读工作台，旧 SQLite 页面接口不再被前端调用。
- AZListing MCP 内部机器合同已建立，外部 Owner 合同状态仍是
  `PENDING_OWNER_FREEZE`。
- 旧 Result Sink 与 Feedback 的正式合同尚未完成真实端到端冻结和验收，因此继续保持关闭。
- OpsLoop 中控 MCP 已切换到正式地址，并以独立 Worker 投递已生成的中控 Outbox。

## 主要文档

- `docs/07-巡检AgentV2最终改造实施方案.md`：唯一正式实施范围。
- `docs/09-MySQL测试库部署报告.md`：测试库部署和回滚边界。
- `docs/14-S5可靠投递与反馈实施报告.md`：Outbox、Inbox 与复扫闭环。
- `docs/15-S6上线运维Runbook.md`：预检、告警、灰度和回滚。
- `docs/16-外部合同冻结与E2E联调清单.md`：外部联调门禁。
- `docs/19-巡检V2数据库表结构与字段说明.md`：13 张 MySQL 表与字段。
- `docs/21-原始事实MySQL归档与复用实施报告.md`：原始 MCP 事实落库与显式复用。
