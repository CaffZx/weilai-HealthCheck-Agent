# 本次审计 MCP 调用情况总结

> 记录日期：2026-08-06  
> 项目：`weilai-HealthCheck-Agent-v2.0`  
> 范围：经营单元身份、中控对接、事实抓取、异步子体事实与 Signal 生命周期复核

## 1. 结论

本次审计没有调用任何线上 MCP、ERP、Amazon API 或中控接口，也没有写入生产数据库。

本次实际执行内容只有：

- 读取项目源码、配置、合同、测试和既有脱敏审计文件；
- 使用 `FakeMcpClient` 和本地构造数据运行离线测试；
- 运行事实归一化与 Signal 生命周期的最小复现；
- 读取 2026-08-05 已留存的中控投递回执进行身份口径核对；
- 连接开发 MySQL 测试库运行全量测试，共收集 488 项，488 项通过；未调用线上 MCP。

因此，本文中的“项目 MCP 调用情况”是对当前代码设计、离线调用链和既有回执的总结，不能视为本次重新进行了线上验收。

## 2. 本次实际 MCP 调用统计

| 类型 | 实际调用次数 | 说明 |
|---|---:|---|
| AZListing / ERP MCP | 0 | 未连接真实 MCP，仅使用离线替身和既有样本 |
| 负责人目录 MCP | 0 | 只检查调用代码和身份映射 |
| 经营模式 MCP | 0 | 只检查 `get_current_operating_mode` 参数与回包校验 |
| 中控 MCP | 0 | 只读取历史回执，没有重新调用 `submit_patrol_batch` |
| 开发 MySQL 测试库 | 已连接 | 执行 Repository、队列、续租、巡检主链路、Raw Fact、Snapshot、Signal 与 Outbox 验证 |
| 生产数据库 | 0 | 未连接、未查询、未写入生产数据库 |

## 3. 当前项目的 MCP 调用链

```text
经营单元发现 / 负责人目录
→ 生成 shopId + parentAsin + parentSellerSku 业务键
→ 父级核心及辅助事实采集
→ 根据 product_info 识别子 ASIN / Seller SKU
→ 异步子体事实采集与缓存
→ 归一化、质量检查、事实快照
→ 确定性巡检规则与 Signal 对账
→ 可选查询当前经营模式
→ Outbox 调用中控 submit_patrol_batch
```

中控业务键已经统一为：

```text
shopId + parentAsin + parentSellerSku
```

`siteCode` 仍用于站点取数和前台数据查询，但不参与经营单元唯一性。

## 4. 经营单元与负责人 MCP

| 工具 | 用途 | 主要入参 | 身份要求 |
|---|---|---|---|
| `erp_amazon_listing_query_page` | 分页发现经营单元 | `pageNo`, `pageSize` | 回包必须包含店铺、站点、父 ASIN、父 SKU、状态等字段 |
| `erp_listing_follow_up_by_principal` | 按负责人发现经营单元 | `principalName`, `shopAccount`, `pageNo`, `pageSize` | 回包父 ASIN、父 SKU 必须匹配受管目录 |
| `sys_user_query` | 获取系统用户与角色 | 分页参数 | 负责人 ID 必须为有效正整数 |
| `sprout_shop_query` | 获取店铺 ID、账号和站点 | `platformCode=Amazon`、分页参数 | 店铺账号必须唯一映射到店铺与站点 |

负责人目录内部使用 `(shop_id, parent_asin, parent_seller_sku)` 匹配受管经营单元，不再使用站点作为唯一键组成部分。

## 5. 父级事实 MCP

每个经营单元默认构造 12 类父级事实调用。

| 内部事实键 | MCP 工具 | 主要用途 | 核心性 |
|---|---|---|---|
| `product_info` | `erp_listing_product_info` | 子体清单、Seller SKU、父子关系、内容属性 | 核心 |
| `listing_identity` | `erp_asin_full_detail` | 父体前台链接、图片、A+、评分等 | 核心 |
| `gross_profit` | `erp_listing_gross_profit_history` | 销量、订单、销售额、广告和毛利历史 | 核心 |
| `stock` | `erp_listing_stock_alert` | 可售、在途、预留和库存风险 | 核心 |
| `business_report` | `erp_listing_business_report` | 会话、转化和订单商品数 | 辅助 |
| `natural_ad_flow` | `erp_listing_natural_advert_flow` | 自然与广告流量结构 | 辅助 |
| `unit_profit` | `erp_listing_unit_profit_analysis` | 单位利润、目标 ACOS 和预算 | 辅助 |
| `inventory_cost` | `erp_listing_inventory_cost_analysis` | 超龄库存、仓储费用 | 辅助 |
| `advert_config` | `erp_listing_advert_agent_config` | 广告目标配置 | 辅助 |
| `monthly_goal` | `erp_listing_monthly_goal` | 当月及未来目标 | 辅助 |
| `refund_rate` | `erp_listing_refund_rate` | 退款率事实 | 辅助 |
| `platform_activity` | `erp_listing_platform_activity` | 平台活动与促销配置 | 辅助 |

父级 ERP 查询会携带 `shopAccount + parentAsin + parentSellerSku`。部分 ERP 历史工具的父 SKU 参数名仍为 `parentSeller`，其余使用 `parentSellerSku`；这是工具合同差异，不代表身份口径不同。

日期型事实统一查询巡检日前已经结束的完整自然日：默认 30 天，窗口截止 `as_of - 1 天`。

## 6. 子体事实 MCP

系统从 `product_info` 返回的每个 `child ASIN + seller SKU` 构造以下调用：

| 内部事实域 | MCP 工具 | 主要参数 |
|---|---|---|
| `child_detail` | `erp_asin_full_detail` | `asin`, `siteCode`, `includeReviews=false` |
| `price_promotion` | `erp_listing_price_promotion_analysis` | `asin`, `siteCode` |
| `recent_reviews` | `erp_listing_review_analysis` | `asin`, `siteCode`, `days=14` |
| `account_health` | `erp_amazon_account_health_issue` | `shopAccount`, `asin`, 日期窗口 |
| `keyword_rank` | `erp_listing_asin_keyword_rank_history` | `asin`, 首个通用关键词、`siteCode`、14 天窗口 |

当前组合根设置为 `synchronous_extensions=False`，即父级巡检不会同步等待所有子体调用，而是：

1. 生成独立子体任务；
2. Worker 低并发调用 MCP；
3. 成功响应写入带有效期的子体事实快照；
4. 后续巡检加载未过期快照；
5. 尚未完成的子体事实登记为 `ASYNC_CHILD_FACT_PENDING`。

子体任务幂等键为：

```text
sha256(operating_unit_id + child_asin + domain)
```

## 7. 经营模式 MCP

可选工具：`get_current_operating_mode`。

调用参数严格使用：

```json
{
  "shop_id": "<shopId>",
  "parent_asin": "<parentAsin>",
  "parent_seller_sku": "<parentSellerSku>"
}
```

回包必须回显同一组业务键；任何店铺、父 ASIN 或父 SKU 不一致都会作为经营单元错配拒绝。当前配置中的 `operating_mode_lookup_enabled` 默认关闭。

## 8. 中控 MCP

投递工具：`submit_patrol_batch`。

调用通过 Outbox 执行，具有内容哈希、重试次数、锁与回执保存。中控请求中每个经营单元的业务键为：

```text
listing.shopId + listing.parentAsin + listing.parentSellerSku
```

中控回执必须完整回显请求中的业务键集合；缺少、增加或错配任何经营单元都会被客户端拒绝。

本次只读复核读取了两个既有回执文件：

- Probe 阶段：1 个经营单元，成功 1，失败 0；
- Remaining 阶段：19 个经营单元，成功 19，失败 0；
- 合计：20 个经营单元，历史回执全部成功；
- 回执包含 `shopId`、`parentAsin`、`parentSellerSku`、`listingId`、`proposalId`；
- 回执没有使用 `siteCode` 作为中控唯一键回显字段。

这组既有回执支持当前三字段业务键方向，但不代表本次审计重新调用或重新验收了中控 MCP。

## 9. 调用结果如何进入巡检

每次 MCP 调用结果按以下方式处理：

- 请求参数和响应先做敏感字段清洗；
- 成功调用保存请求哈希、完整清洗后响应、抽取数据、内容哈希、行数和采集时间；
- 失败调用保存错误码和错误信息，不伪造空响应；
- 核心事实失败形成阻断缺口；
- 辅助事实失败形成 `PARTIAL` 快照并继续输出可判定部分；
- 原始事实、归一化快照、Signal 和中控载荷通过 Run、Snapshot、Raw Fact 与哈希关联。

## 10. 本次复核发现的调用链风险与处理状态

### 已修复：PARTIAL 不再用于确认异常消失

只有 `COMPLETE` 快照会向 Signal 对账器传入 `facts_complete=True`。`PARTIAL`、`INSUFFICIENT` 和 `FAILED` 均不能让旧业务异常累计 `MISSED/RECOVERED`；数据质量 Signal 仍按自身事实恢复。

### 已修复：单位利润缺失成本时不再按零计算

`unit_profit` 没有直接返回 `unitProfit` 时，只有售价、佣金、产品成本、FBA 费用和广告费率全部存在才允许推导；任一组件缺失即保留 `None` 并登记数据缺口。

### 已修复：截止日之后的销售记录被隔离

请求窗口和归一化层现在双重执行完整自然日口径。`stat_date > 巡检日前一天` 的记录不会进入 3/7/30 天指标，并登记 `sales.rows_after_data_cutoff`。

### 已修复：重复子 ASIN 和 Seller SKU 冲突被合同拒绝

`product_info` 返回相同子 ASIN 多次时会产生 `PRODUCT_CHILD_ASIN_DUPLICATE`；同一子 ASIN 对应不同 Seller SKU 时同时产生 `PRODUCT_CHILD_SKU_CONFLICT`，归一化前终止该经营单元。

### P1：身份迁移可能破坏待投递载荷哈希

`0007_control_center_business_key` 会替换历史 JSON 中的经营单元 ID，但只针对内部巡检 Outbox 重算哈希。若待投递中控载荷包含被替换的 ID，迁移后可能被中控 Worker 判定为内容哈希不一致。

## 11. 验证记录

| 验证项 | 结果 |
|---|---|
| 中控三字段身份模型与合同 | 通过 |
| 经营模式 MCP 三字段查询与回显校验 | 通过源码及单元测试复核 |
| 父级、子体事实调用参数构造 | 通过源码及离线测试复核 |
| 历史 20 单元中控回执 | 20 成功、0 失败 |
| 全量测试 | 488 通过、0 失败（已配置开发 MySQL 测试库） |
| 真实 MySQL 运行验证 | 通过；schema revision 为 `0010_product_image_cache` |
| 历史异常任务 | 8 个批次中的 21 个 `DEAD/PENDING` 已归档，966 个成功任务保留 |
| 运行预检 | `HEALTHY`，无 `DEAD/PENDING/RUNNING` 任务 |
| 静态检查 | Ruff 全范围通过，0 问题 |
| 本次真实线上 MCP 调用 | 0 |

## 12. 建议处理顺序

1. 审计 `0007_control_center_business_key` 对历史待投递中控 Outbox 的哈希处理；
2. 等子体异步任务完成后，用真实经营单元重新统计子体事实和巡检点位覆盖率；
3. 进行一轮真实只读 MCP、MySQL 落库与中控联调验收。

## 13. 证据位置

- 父级及子体调用构造：`facts/collector.py`
- MCP 输入合同：`clients/azlisting_contract.py`
- 经营模式 MCP：`clients/operating_mode_mcp.py`
- 中控 MCP：`clients/control_center_mcp.py`
- 负责人目录：`integrations/owner_directory.py`
- 异步子体事实：`integrations/child_fact_async.py`
- 事实归一化与质量：`facts/normalizer.py`、`facts/quality.py`
- Signal 对账调用：`inspector/patrol_orchestrator.py`、`inspector/signal_reconciler.py`
- 身份迁移：`alembic/versions/0007_control_center_business_key.py`
- 历史中控回执：`artifacts/control-center-delivery/`
