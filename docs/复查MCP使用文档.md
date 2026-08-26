# 复查 MCP 使用文档（对外）

适用对象：中控 Agent、其他需要调用“经营单元复盘”的调用方。

## 1. 用途

复查 MCP 用于对一个已完成巡检的经营单元执行复盘：读取原始巡检基线，重新采集当前事实，按中控提交的成功/失败标准和执行回执计算结论，并返回复盘结果。

复查只读事实、判断结果，不会执行广告、Listing 或库存操作。

## 2. 服务入口与鉴权

对外入口（正式环境）：

```text
http://36.140.52.167:8020/mcp
```

鉴权方式：请求头携带 Bearer Token。

```http
Authorization: Bearer <REVIEW_MCP_PUBLIC_TOKEN>
Accept: application/json, text/event-stream
```

- Token 由巡检 Agent 部署负责人提供，独立于 AZListing 的 `MCP_API_KEY` 和中控的 `CONTROL_CENTER_MCP_TOKEN`。
- 未携带 Token 或 Token 错误时返回 `401 Unauthorized`。
- 内部复查服务监听 `127.0.0.1:8791`，不直接对外暴露，调用方只访问 `8020/mcp`。

## 3. 工具与参数结构

工具名：

```text
review_patrol_result
```

使用 MCP SDK 调用时，参数必须放在 `payload` 对象里：

```json
{
  "payload": {
    "contractVersion": "amazon_ops.patrol_review_request.v1",
    "...": "ReviewRequest 字段"
  }
}
```

不能把 `ReviewRequest` 字段平铺到调用参数顶层，否则会返回 `payload Field required`。

## 4. 请求字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `contractVersion` | 否 | 固定 `amazon_ops.patrol_review_request.v1` |
| `reviewRequestId` | 是 | 复查请求号，最长 128 字符 |
| `reviewRound` | 是 | 复查轮次，从 1 开始 |
| `patrolBatchNo` | 是 | 原巡检批次号 |
| `proposalId` | 是 | 中控建议 ID |
| `baselineSnapshotId` | 是 | 原巡检基线快照 ID，格式 `fs_` + 24 位小写十六进制 |
| `shopId` | 是 | 店铺 ID，正整数 |
| `shopAccount` | 是 | ERP 店铺账号 |
| `siteCode` | 是 | 站点，例如 `AMAZON_US` |
| `parentAsin` | 是 | 父 ASIN |
| `parentSellerSku` | 是 | 父 Seller SKU |
| `approvedPlan` | 是 | 中控批准的执行计划对象 |
| `executionReceipts` | 否 | 执行回执数组 |
| `successCriteria` | 否 | 成功判断标准数组 |
| `failureCriteria` | 否 | 失败判断标准数组 |
| `requestedAt` | 是 | ISO 8601 时间，如 `2026-08-08T00:00:00Z` |

### Criterion 格式

```json
{
  "criterionId": "facts-collected",
  "metricPath": "sales.row_count",
  "operator": "GTE",
  "target": 1
}
```

支持的 `operator`：`GTE`、`LTE`、`EQ`。

## 5. 调用示例

以下为 MCP Python SDK 调用示例。真实调用时替换 Token、地址和业务字段：

```python
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

request = {
    "contractVersion": "amazon_ops.patrol_review_request.v1",
    "reviewRequestId": "REVIEW-20260808-0001",
    "reviewRound": 1,
    "patrolBatchNo": "PATROL-BATCH-0001",
    "proposalId": "PROPOSAL-0001",
    "baselineSnapshotId": "fs_0123456789abcdef01234567",
    "shopId": 1562,
    "shopAccount": "am_example_us",
    "siteCode": "AMAZON_US",
    "parentAsin": "B0EXAMPLE0",
    "parentSellerSku": "FS04026-zhu",
    "approvedPlan": {"items": []},
    "executionReceipts": [],
    "successCriteria": [
        {
            "criterionId": "facts-collected",
            "metricPath": "sales.row_count",
            "operator": "GTE",
            "target": 1,
        }
    ],
    "failureCriteria": [],
    "requestedAt": "2026-08-08T00:00:00Z",
}


async def main():
    headers = {
        "Authorization": f"Bearer {os.environ['REVIEW_MCP_PUBLIC_TOKEN']}",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(30, read=180)
    ) as client:
        async with streamable_http_client(
            "http://36.140.52.167:8020/mcp", http_client=client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(
                    "review_patrol_result",
                    {"payload": request},
                )
                print(result)


asyncio.run(main())
```

## 6. 幂等规则（重要）

幂等键是 `reviewRequestId + reviewRound`。

- 相同键 + 相同请求内容：直接返回已保存的复查结果，不会重新采集。
- 相同键 + 请求内容不同：返回幂等冲突，不会覆盖原结果。
- `requestedAt` 不参与幂等比对：重试时重新生成 `requestedAt` 不会冲突，会直接返回已保存结果。
- 幂等比对只针对业务内容：`approvedPlan`、`executionReceipts`、成功/失败标准必须原样复用首次请求。
- 需要按新回执、新标准或新时间重新复查时：保持同一个 `reviewRequestId`，将 `reviewRound` 加一（例如从 `1` 改为 `2`）；或者生成新的 `reviewRequestId` 并从第 1 轮开始。
- 同一复查正在处理时再次提交，返回“正在处理”冲突。

## 7. 返回结果

核心字段：

| 字段 | 说明 |
|---|---|
| `outcomeStatus` | `SUCCESS`、`PARTIAL_SUCCESS`、`FAILED`、`INCONCLUSIVE` |
| `currentFactSnapshot` | 本次重新采集并保存的事实快照 |
| `successResults` | 成功标准逐条判断结果 |
| `failureResults` | 失败标准逐条判断结果 |
| `dataGaps` | 当前复查的数据缺口 |
| `executionDeviations` | 批准计划与执行回执的差异 |
| `requiresNewPatrol` | 是否需要重新发起巡检 |
| `nextRecommendation` | `CLOSE_CYCLE`、`START_NEW_PATROL_CYCLE`、`REPAIR_DATA` |
| `reviewedAt` | 复查完成时间 |

结论规则：

- 数据阻断或标准无法计算 → `INCONCLUSIVE`，建议 `REPAIR_DATA`；
- 任一失败标准满足 → `FAILED`，建议 `START_NEW_PATROL_CYCLE`；
- 所有成功标准满足且无执行偏差 → `SUCCESS`，建议 `CLOSE_CYCLE`；
- 部分成功标准满足 → `PARTIAL_SUCCESS`。

## 8. 错误处理

| 错误 | 含义与处理 |
|---|---|
| `401 Unauthorized` | Token 缺失或错误；向部署负责人获取 `REVIEW_MCP_PUBLIC_TOKEN` |
| `503 MCP service is unavailable` | 对外代理未启动或复查服务未运行；检查 API 服务与复查服务 |
| `payload Field required` | 请求参数必须包在 `payload` 对象里 |
| `reviewRequestId and reviewRound identify one immutable review request...` | 幂等冲突；原样重试原请求，或递增 `reviewRound` 后再提交新内容 |
| `review request is already processing` | 同一复查正在处理，稍后重试原请求 |
| 基线快照不存在或不属于该经营单元 | 请求的 `baselineSnapshotId` 或经营单元身份有误 |

## 9. 验证状态

- 对外入口 `http://36.140.52.167:8020/mcp` 已验证可访问。
- 无 Token 访问返回 `401`，正确 Token 调用返回 `200`。
- 真实数据库基线 + 真实 AZ MCP 复查成功。
- 重复提交相同请求命中幂等缓存，不会重复创建复查记录。

## 10. 服务开关

服务器当前配置：

```text
REVIEW_MCP_ENABLED=true
REVIEW_MCP_HOST=127.0.0.1
REVIEW_MCP_PORT=8791
REVIEW_MCP_UPSTREAM_URL=http://127.0.0.1:8791/mcp
```

关闭复查时由部署负责人设置 `REVIEW_MCP_ENABLED=false` 并重启复查服务。
