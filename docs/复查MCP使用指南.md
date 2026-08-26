# 复查 MCP 使用指南

## 1. 用途

复查 MCP 由中控在一个已完成的巡检建议执行后调用。巡检侧会：

1. 根据 `baselineSnapshotId` 读取原始巡检基线；
2. 使用请求中的经营单元身份重新调用事实 MCP；
3. 归一化并保存当前事实快照；
4. 按成功标准、失败标准和执行回执计算复查结论；
5. 返回 `ReviewResult`，并将结果幂等保存到 `t_patrol_review`。

复查不会执行广告、Listing 或库存操作，只读取事实并判断结果。

## 2. 入口与鉴权

### 服务器入口

```text
http://36.140.52.167:8020/mcp
```

该入口监听服务器对外的 `8020` 端口，是带鉴权的代理，内部转发到：

```text
http://127.0.0.1:8791/mcp
```

内部复查 MCP 仍只绑定服务器本机回环地址，不直接暴露 `8791` 端口。中控和其他调用方只访问带 Bearer 鉴权的 `8020/mcp`。

### 请求头

```http
Authorization: Bearer <REVIEW_MCP_PUBLIC_TOKEN>
Accept: application/json, text/event-stream
```

调用方除了服务器地址，还必须从巡检 Agent 部署负责人处获取当前服务器配置的
`REVIEW_MCP_PUBLIC_TOKEN`。拿到令牌后，在调用进程中设置：

```bash
export REVIEW_MCP_PUBLIC_TOKEN='<部署负责人提供的实际令牌>'
```

Python 示例会从该环境变量读取令牌：

```python
token = os.environ["REVIEW_MCP_PUBLIC_TOKEN"]
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json, text/event-stream",
}
```

未携带令牌或令牌错误时，服务器会返回 `401 Unauthorized`。服务器地址和 Token
缺一不可；Token 不随本文档保存或传播，需要由部署负责人单独提供给获准调用的中控。

复查令牌必须独立于 AZListing 的 `MCP_API_KEY` 和中控的 `CONTROL_CENTER_MCP_TOKEN`。

### 调用地址

中控或其他调用方使用 `http://36.140.52.167:8020/mcp`。不要直接访问服务器内部的 `8791` 端口；如果服务器公网地址发生变化，需要同步更新此处地址。

## 3. 工具

```text
review_patrol_result
```

使用 MCP SDK 调用时，工具参数必须是：

```json
{
  "payload": {
    "contractVersion": "amazon_ops.patrol_review_request.v1",
    "...": "ReviewRequest 字段"
  }
}
```

不能直接把 `ReviewRequest` 字段平铺到 `tools/call.params.arguments`，否则会收到 `payload Field required`。

## 4. 请求字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `contractVersion` | 否 | 固定为 `amazon_ops.patrol_review_request.v1` |
| `reviewRequestId` | 是 | 复查请求号，最长 128 字符 |
| `reviewRound` | 是 | 复查轮次，从 1 开始 |
| `patrolBatchNo` | 是 | 原巡检批次号 |
| `proposalId` | 是 | 中控建议 ID |
| `baselineSnapshotId` | 是 | 原巡检基线快照，格式 `fs_` + 24 位小写十六进制字符 |
| `shopId` | 是 | 店铺 ID，正整数 |
| `shopAccount` | 是 | ERP 店铺账号 |
| `siteCode` | 是 | 站点，例如 `AMAZON_US` |
| `parentAsin` | 是 | 父 ASIN |
| `parentSellerSku` | 是 | 父 Seller SKU |
| `approvedPlan` | 是 | 中控批准的执行计划对象 |
| `executionReceipts` | 否 | 执行回执数组，用于判断执行偏差 |
| `successCriteria` | 否 | 成功判断标准数组 |
| `failureCriteria` | 否 | 失败判断标准数组 |
| `requestedAt` | 是 | ISO 8601 时间 |

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

以下示例使用 MCP Python SDK。真实调用时，将地址、令牌和业务字段替换为当前值：

```python
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

request = {
    "contractVersion": "amazon_ops.patrol_review_request.v1",
    "reviewRequestId": "REVIEW-20260807-0001",
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
    "requestedAt": "2026-08-07T00:00:00Z",
}


async def main():
    headers = {
        "Authorization": f"Bearer {os.environ['REVIEW_MCP_PUBLIC_TOKEN']}",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=180)) as client:
        async with streamable_http_client("http://36.140.52.167:8020/mcp", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(
                    "review_patrol_result",
                    {"payload": request},
                )
                print(result)


asyncio.run(main())
```

## 6. 返回结果

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

### 两条真实可复用测试样本

以下两条已经通过服务器入口和真实 AZ MCP 验证。重复提交相同请求会命中幂等缓存，不会重新采集；如需真正重新复查，请生成新的 `reviewRequestId` 或增加 `reviewRound`，并同步更新 `requestedAt`。

#### 样本 1：B0EXAMPLE0

调用参数：

```json
{
  "payload": {
    "contractVersion": "amazon_ops.patrol_review_request.v1",
    "reviewRequestId": "REAL-REVIEW-COMPLETE-20260807-01",
    "reviewRound": 1,
    "patrolBatchNo": "REAL-REVIEW-COMPLETE-BATCH-20260807-01",
    "proposalId": "REAL-REVIEW-COMPLETE-PROPOSAL-01",
    "baselineSnapshotId": "fs_8b93be8227714c338a28e308",
    "shopId": 1596,
    "shopAccount": "am_example_us",
    "siteCode": "AMAZON_US",
    "parentAsin": "B0EXAMPLE0",
    "parentSellerSku": "RJ-FS03015-1PCS",
    "approvedPlan": {"items": []},
    "executionReceipts": [],
    "successCriteria": [
      {
        "criterionId": "facts-collected",
        "metricPath": "sales.row_count",
        "operator": "GTE",
        "target": 1
      }
    ],
    "failureCriteria": [],
    "requestedAt": "2026-08-07T04:25:53.703722Z"
  }
}
```

已验证结果：

```json
{
  "outcomeStatus": "SUCCESS",
  "currentSnapshotId": "fs_9c1bf06cb7e34bb68b68c08b",
  "qualityStatus": "PARTIAL",
  "completenessScore": 0.8,
  "ownerUserName": "某某",
  "requiresNewPatrol": false,
  "nextRecommendation": "CLOSE_CYCLE"
}
```

#### 样本 2：B0EXAMPLE0

调用参数：

```json
{
  "payload": {
    "contractVersion": "amazon_ops.patrol_review_request.v1",
    "reviewRequestId": "REAL-REVIEW-COMPLETE-20260807-02",
    "reviewRound": 1,
    "patrolBatchNo": "REAL-REVIEW-COMPLETE-BATCH-20260807-02",
    "proposalId": "REAL-REVIEW-COMPLETE-PROPOSAL-02",
    "baselineSnapshotId": "fs_64d39d8de4194341ba7bbb8e",
    "shopId": 1622,
    "shopAccount": "am_example_us",
    "siteCode": "AMAZON_US",
    "parentAsin": "B0EXAMPLE0",
    "parentSellerSku": "tuan-fu-new1",
    "approvedPlan": {"items": []},
    "executionReceipts": [],
    "successCriteria": [
      {
        "criterionId": "facts-collected",
        "metricPath": "sales.row_count",
        "operator": "GTE",
        "target": 1
      }
    ],
    "failureCriteria": [],
    "requestedAt": "2026-08-07T04:26:50.058601Z"
  }
}
```

已验证结果：

```json
{
  "outcomeStatus": "SUCCESS",
  "currentSnapshotId": "fs_5eb8d3c6f54c4ced87d5251e",
  "qualityStatus": "PARTIAL",
  "completenessScore": 0.8,
  "ownerUserName": "某某",
  "requiresNewPatrol": false,
  "nextRecommendation": "CLOSE_CYCLE"
}
```

## 7. 幂等与错误处理

- 幂等键是 `reviewRequestId + reviewRound`；相同键、相同请求内容会直接返回已保存结果；
- 相同键但请求内容不同，会返回幂等冲突；
- `requestedAt` 不参与幂等比对：中控重试时重新生成 `requestedAt` 不会冲突，会直接返回已保存结果；
- 幂等比对只针对业务内容：`approvedPlan`、`executionReceipts`、成功/失败标准必须原样复用首次请求；
- 需要按新的执行回执、标准重新复查时，保持同一个 `reviewRequestId` 并将 `reviewRound` 加一（例如从 `1` 改为 `2`）；也可以生成新的 `reviewRequestId` 并从第 `1` 轮开始。
- 同一复查正在处理时再次提交，会返回处理中冲突；
- 基线快照不存在或不属于该经营单元，会失败，不会伪造当前结果；
- MCP 事实失败会记录在 `dataGaps`，是否阻断由事实质量规则决定；
- 复查结果和当前快照会写入 MySQL 的 `t_patrol_review`、`t_patrol_fact_snapshot`。

## 8. 当前验证状态

已验证：

- 工具发现：`review_patrol_result`；
- 无令牌访问返回 `401`；
- 真实数据库基线 + 真实 AZ MCP 复查成功；
- 示例经营单元 `B0EXAMPLE0 / FS04026-zhu` 返回 `SUCCESS`；
- 两条完整度 `80%` 的真实样本 `B0EXAMPLE0`、`B0EXAMPLE0` 均返回 `SUCCESS / CLOSE_CYCLE`；
- 重复提交命中幂等结果，未重复创建复查记录。

当前本地服务开关：

```yaml
feature_flags.review_mcp_enabled: true
REVIEW_MCP_ENABLED=true
```

Feedback 接收和到期自动复扫是独立链路，当前仍未开启。
