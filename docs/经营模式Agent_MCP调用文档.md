# 经营模式 Agent MCP 调用文档

## 接入信息

```text
地址：http://10.0.0.0:8001/mcp
协议：MCP Streamable HTTP
鉴权：Authorization: Bearer <b09486fef6622372>
```


## 第三方配置

MCP 客户端配置示例：

```json
{
  "servers": {
    "amazon-operating-mode-agent": {
      "url": "http://10.0.0.0:8001/mcp",
      "headers": {
        "Authorization": "Bearer <通过安全渠道获得的 Token>"
      }
    }
  }
}
```

初始化后调用 `tools/list` 可看到 5 个 Tool：

```text
evaluate_operating_mode
get_current_operating_mode
get_current_operating_modes
start_operating_mode_evaluation_job
get_operating_mode_evaluation_job
```

新版服务另外提供 `start_full_operating_mode_evaluation`、
`get_full_operating_mode_evaluation` 和同步单经营单元工具 `get_evaluate`。
巡检批量投递仍使用下方的“批量查询 → 自动评估 → 复查”只读闭环；需要单条同步刷新时，
客户端使用 `OperatingModeMcpClient.get_evaluate()` 调用 `get_evaluate`。

## Tool

以下各 Tool 小节中的“返回示例”表示业务数据对象。通过 Streamable HTTP 直接调用 JSON-RPC 时，MCP 服务会将业务对象放在 `result.structuredContent`，同时在 `result.content` 中提供文本形式：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{...与 structuredContent 相同的 JSON 文本...}"
      }
    ],
    "structuredContent": {
      "...": "下文定义的业务返回字段"
    },
    "isError": false
  }
}
```

使用 MCP SDK 时应优先读取 `structured_content` / `structuredContent`，不要依赖 `content[0].text` 二次解析。Tool 执行失败时 `isError=true`，错误说明位于 `content`，不会返回正常业务对象。

### `evaluate_operating_mode`

执行 G0-G8 经营模式判断。该 Tool 会写入数据库，包括不可变 `fact_snapshot`、判断历史和当前判断。

参数：

```json
{
  "request": {
    "request_id": "REQ-001",
    "trace_id": "TRACE-001",
    "contract_version": "1.0",
    "operating_unit": {
      "operating_unit_id": "OU-001",
      "shop_account": "shop-account",
      "shop_id": "1622",
      "site_code": "Amazon_US",
      "parent_asin": "B0ABC12345",
      "parent_seller_sku": "SKU-001"
    },
    "fact_snapshot": {
      "snapshot_id": "FS-001",
      "snapshot_version": 1,
      "as_of_time": "2026-08-06T00:00:00Z",
      "content_hash": "sha256-value",
      "identity": {},
      "sales": {},
      "traffic": {},
      "profit": {},
      "inventory": {},
      "price": {},
      "quality": {},
      "execution_history": {},
      "source_refs": []
    },
    "policy_context": {
      "rule_version": "mode_rules_v1",
      "progress_preparation": "PROGRESS_PERIOD",
      "season_posture": "NORMAL",
      "approved_threshold_profile": "US_STANDARD_V1",
      "cash_recovery_assumptions": {"scenarios": []}
    },
    "options": {
      "include_explanation": true,
      "strict_evidence": true
    }
  }
}
```

返回示例：

```json
{
  "decision_id": "MD-7B1D...",
  "request_id": "REQ-001",
  "trace_id": "TRACE-001",
  "operating_unit_id": "OU-001",
  "fact_snapshot_id": "FS-001",
  "decision_status": "DECIDED",
  "recommended_mode_code": "STABLE_OPERATION",
  "recommended_mode": "稳定经营",
  "mode_variant": null,
  "scope_type": "PARENT",
  "confidence": 0.92,
  "gate_results": [
    {
      "gate_id": "G0",
      "status": "PASS",
      "reason_codes": [],
      "evidence": {}
    }
  ],
  "key_metrics": {
    "best_cash": 258.0,
    "parent_unit_contribution": 2.0
  },
  "reason_codes": ["BASELINE_HEALTHY"],
  "excluded_modes": [],
  "data_gaps": [],
  "child_disposition_candidates": [],
  "required_guardrails": [],
  "observation_requirements": [],
  "explanation": "经营模式判断说明",
  "rule_version": "mode_rules_v1",
  "producer_version": "1.0.0",
  "generated_at": "2026-08-06T08:00:00Z"
}
```

返回字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `decision_id` | string | 本次判断的唯一 ID |
| `request_id` | string | 调用方请求 ID，也是幂等键 |
| `trace_id` | string | 全链路追踪 ID |
| `operating_unit_id` | string | 经营单元内部标识 |
| `fact_snapshot_id` | string | 本次判断使用的不可变事实快照 ID |
| `decision_status` | string | `DECIDED`、`BLOCKED` 或 `NEEDS_HUMAN_REVIEW` |
| `recommended_mode_code` | string/null | 经营模式英文代码；阻断或需人工复核时可能为 `null` |
| `recommended_mode` | string/null | 经营模式中文名称；与英文代码同步为空或有值 |
| `scope_type` | string | `PARENT`、`CHILD_ONLY`、`MIXED` 或 `UNKNOWN` |
| `confidence` | number | 判断置信度，范围 0–1 |
| `gate_results` | array | G0–G8 各 Gate 的状态、原因码和证据 |
| `key_metrics` | object | 判断过程中使用的关键指标；字段随命中 Gate 变化 |
| `reason_codes` | array | 支持最终结论的标准原因码 |
| `excluded_modes` | array | 已被证据排除的经营模式代码 |
| `data_gaps` | array | 缺失或不足的数据项，例如 Cost Ontology 输入不完整 |
| `child_disposition_candidates` | array | 子体级候选处置建议；没有时为空数组 |
| `required_guardrails` | array | 执行该模式必须遵守的保护条件 |
| `observation_requirements` | array | 后续必须持续观察的指标或事件 |
| `explanation` | string | 面向业务方的判断说明 |
| `rule_version` | string | 使用的经营模式规则版本 |
| `producer_version` | string | 判断服务版本 |
| `generated_at` | datetime | 判断生成时间，ISO 8601 |

相同 `request_id` 对应不同请求内容时返回 `IDEMPOTENCY_CONFLICT`。

### `get_current_operating_mode`

按单个业务键查询当前判断。该 Tool 只读、不重新执行判断。

```json
{
  "shop_id": "1622",
  "parent_asin": "B0ABC12345",
  "parent_seller_sku": "SKU-001"
}
```

命中时返回：

```json
{
  "shop_id": "1622",
  "parent_asin": "B0ABC12345",
  "parent_seller_sku": "SKU-001",
  "decision_status": "DECIDED",
  "recommended_mode_code": "STABLE_OPERATION",
  "recommended_mode": "稳定经营",
  "explanation": "经营模式判断说明",
  "current_sale_cash_recovery": 258.0,
  "parent_unit_contribution": 2.0,
  "currency": "CNY"
}
```

没有当前判断记录时返回 `null`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `shop_id` / `parent_asin` / `parent_seller_sku` | string | 经营单元业务键 |
| `decision_status` | string | 当前判断状态 |
| `recommended_mode_code` | string/null | 经营模式英文代码 |
| `recommended_mode` | string/null | 经营模式中文名称 |
| `explanation` | string | 当前判断说明 |
| `current_sale_cash_recovery` | number/null | 当前销售情景的库存净现金回收总额；没有完整情景时为 `null` |
| `parent_unit_contribution` | number/null | 父体单件贡献利润；事实快照无该数据时为 `null` |
| `currency` | string | 固定为 `CNY` |

### `get_current_operating_modes`

批量查询当前判断。该 Tool 只读、不重新执行判断。单次 1–10000 个经营单元，未命中项放入 `missing_operating_units`。

```json
{
  "operating_units": [
    {
      "shop_id": "1622",
      "parent_asin": "B0ABC12345",
      "parent_seller_sku": "SKU-001"
    }
  ]
}
```

返回示例：

```json
{
  "requested_count": 2,
  "found_count": 1,
  "missing_count": 1,
  "results": [
    {
      "shop_id": "1622",
      "parent_asin": "B0ABC12345",
      "parent_seller_sku": "SKU-001",
      "decision_status": "DECIDED",
      "recommended_mode_code": "STABLE_OPERATION",
      "recommended_mode": "稳定经营",
      "explanation": "经营模式判断说明",
      "current_sale_cash_recovery": 258.0,
      "parent_unit_contribution": 2.0,
      "currency": "CNY"
    }
  ],
  "missing_operating_units": [
    {
      "shop_id": "1622",
      "parent_asin": "B000000000",
      "parent_seller_sku": "MISSING-SKU"
    }
  ]
}
```

`results` 只包含已命中记录；未命中业务键进入 `missing_operating_units`，不会生成空结果占位。

### `start_operating_mode_evaluation_job`

创建批量异步抓取与经营判断任务。该 Tool 会写入任务表，并由后台 Worker 抓取数据、冻结事实、同步执行 G0–G8、保存历史和当前判断。

```json
{
  "operating_units": [
    {
      "shop_id": "1622",
      "parent_asin": "B0ABC12345",
      "parent_seller_sku": "SKU-001"
    }
  ]
}
```

单次 1–100 个唯一经营单元。返回：

```json
{
  "job_id": "JOB-001",
  "status": "PENDING",
  "requested_count": 1,
  "succeeded_count": 0,
  "failed_count": 0
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `job_id` | string | 后续查询任务的唯一 ID |
| `status` | string | 创建成功时通常为 `PENDING` |
| `requested_count` | integer | 本批提交的经营单元数量 |
| `succeeded_count` | integer | 已成功完成判断的数量 |
| `failed_count` | integer | 已失败的经营单元数量 |

该 Tool 只表示任务已经持久化，不表示数据抓取和判断已经完成。上游未配置或当前实例未启用 Worker 时返回 `UPSTREAM_NOT_CONFIGURED`。

### `get_operating_mode_evaluation_job`

按 `job_id` 查询任务状态、成功结果和失败明细。该 Tool 只读、不重新执行判断。

```json
{
  "job_id": "JOB-001"
}
```

成功结果按输入顺序组织，并通过任务明细的 `decision_request_id` 精确读取对应历史判断，不读取可能已被其他任务覆盖的 current 记录。`current_sale_cash_recovery` 与 `parent_unit_contribution` 均以 CNY 口径返回。

任务状态：`PENDING`、`RUNNING`、`SUCCEEDED`、`PARTIAL_SUCCESS`、`FAILED`。

运行中返回示例：

```json
{
  "job_id": "JOB-001",
  "status": "RUNNING",
  "requested_count": 2,
  "succeeded_count": 1,
  "failed_count": 0,
  "results": [
    {
      "shop_id": "1622",
      "parent_asin": "B0ABC12345",
      "parent_seller_sku": "SKU-001",
      "decision_status": "DECIDED",
      "recommended_mode_code": "STABLE_OPERATION",
      "recommended_mode": "稳定经营",
      "explanation": "经营模式判断说明",
      "current_sale_cash_recovery": 258.0,
      "parent_unit_contribution": 2.0,
      "currency": "CNY"
    }
  ],
  "failures": []
}
```

部分成功终态示例：

```json
{
  "job_id": "JOB-001",
  "status": "PARTIAL_SUCCESS",
  "requested_count": 2,
  "succeeded_count": 1,
  "failed_count": 1,
  "results": [
    {
      "shop_id": "1622",
      "parent_asin": "B0ABC12345",
      "parent_seller_sku": "SKU-001",
      "decision_status": "DECIDED",
      "recommended_mode_code": "STABLE_OPERATION",
      "recommended_mode": "稳定经营",
      "explanation": "经营模式判断说明",
      "current_sale_cash_recovery": 258.0,
      "parent_unit_contribution": 2.0,
      "currency": "CNY"
    }
  ],
  "failures": [
    {
      "shop_id": "1623",
      "parent_asin": "B0EXAMPLE0",
      "parent_seller_sku": "SKU-002",
      "error_code": "REQUIRED_UPSTREAM_DATA_MISSING",
      "message": "必需上游工具没有业务数据"
    }
  ]
}
```

返回字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `job_id` | string | 任务唯一 ID |
| `status` | string | 任务总体状态 |
| `requested_count` | integer | 请求总数 |
| `succeeded_count` | integer | 成功数，与成功明细状态一致 |
| `failed_count` | integer | 失败数，与失败明细状态一致 |
| `results` | array | 已成功完成判断的结果；运行中允许只返回部分结果 |
| `failures` | array | 已失败经营单元及公开错误；运行中允许只返回部分失败 |
| `failures[].error_code` | string | 稳定的机器可读错误码 |
| `failures[].message` | string | 已脱敏的业务错误说明 |

`PENDING` 或尚无已完成明细的 `RUNNING` 任务会返回空的 `results` 和 `failures`。全成功为 `SUCCEEDED`，成功与失败并存为 `PARTIAL_SUCCESS`，全部失败为 `FAILED`。

## 错误

| 场景 | 返回语义 |
| --- | --- |
| MCP Token 缺失或错误 | HTTP `401` |
| 参数错误 | Tool 参数校验错误 |
| 请求 ID 冲突 | `IDEMPOTENCY_CONFLICT` |
| 当前实例不能创建任务 | `UPSTREAM_NOT_CONFIGURED` |
| 任务不存在 | `EVALUATION_JOB_NOT_FOUND` |
| 身份无匹配 | `OPERATING_UNIT_NOT_FOUND` |
| 身份多匹配 | `OPERATING_UNIT_AMBIGUOUS` |
| 必需上游数据为空 | `REQUIRED_UPSTREAM_DATA_MISSING` |
| 限流重试耗尽 | `UPSTREAM_RATE_LIMIT_EXHAUSTED` |

响应和日志不得包含上游 Token、数据库连接信息、完整原始响应或内部异常堆栈。
