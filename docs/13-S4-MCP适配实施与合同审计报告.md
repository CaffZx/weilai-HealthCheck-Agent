# S4 MCP 适配实施与合同审计报告

> 2026-08-04 补充：第二个 `MCP_SYNC_GATEWAY` 已完成真实只读验收，53 个 Tool 可发现，
> 14 个项目相关查询全部成功。由于用户、店铺和扩展信息响应包含凭据字段，V2 已在
> `McpToolResult` 和 MySQL Repository 增加双层递归脱敏；该网关在字段合同冻结前仅作为
> 补充事实候选，不替换本报告的 AZListing 主事实 Tool。详见
> `docs/22-Streamable-MCP真实只读验收与MySQL接入边界.md`。
> 业务方已确认上述凭据字段属于既定服务合同，不要求服务端整改；V2 只负责不归一化、不落库。
> AZListing 现已启用 `AUDITED_ONLY` 工具策略：客户端只允许 1 个经营单元清单 Tool 和 10 个
> 正式事实 Tool。任何其他 Tool 在建立网络连接前返回 `MCP_TOOL_NOT_ALLOWED`（HTTP 403、不可重试）。

> 日期：2026-07-31  
> 状态：Adapter 与离线验收完成；生产 Scheduler 启用受外部合同阻断  
> 实施基线：`docs/07-巡检AgentV2最终改造实施方案.md`

2026-08-04 已使用测试网关完成真实只读验收：34 个 Tool 可发现，10 个事实 Tool 中 9 个
真实查询成功；清单 Tool 对当前 Key 返回无权限，`erp_asin_full_detail` 父/子 ASIN均在
60 秒超时。完整证据见 `docs/21-AZListing-MCP真实只读验收报告.md`。

## 范围与证据限制

本阶段只读对照了 `mcp-azlisting-server-线上` 33 个 Tool 的工具文档、用户提供的
《巡检 Agent 向运营闭环中控回传数据 MCP 接入文档 v1.0》和本项目代码。未调用生产 MCP，
未把文档中的地址或 Token 写入仓库。用户提供的回传文档定义的是中控写入 Tool
`submit_patrol_batch`，属于 S5 Result Sink，不是 S4 经营单元/事实输入 MCP。

## 已完成

- 新增 `McpOperatingUnitProvider`，严格遍历 `erp_amazon_listing_query_page` 全部分页；
- 校验页码、总页数、总数稳定性、实际遍历条数、重复绑定和空页语义；
- 缺父 ASIN/父 Seller SKU 时整批返回 `MCP_CONTRACT_INVALID`，不拿子 ASIN 冒充父级；
- 单元事实把在线列表分页工具替换为 `erp_asin_full_detail`；
- 事实工具从 8 个扩展为 10 个，新增广告目标配置和 16/32 周退款率；
- 对齐 `recordDate/productSaleNum/orderSaleAmount/adCostAmount/adSaleMoney` 等正式字段；
- 对齐业务报告嵌套 `records[]`、库存费用 `children[].longTermStorageFees[]` 和单件费用字段；
- SourceRef 的销量窗口止点使用实际最新 `recordDate`，不把查询日期冒充数据截止时间；
- 自然/广告流量无日期时只保留窗口聚合，不再按行序伪造逐日趋势；
- MCP `structuredContent.count` 与 `data` 不一致时拒绝；
- 参数/合同错误不重试，限流、超时和传输错误按配置退避；
- 正式组合根已装配 Provider 和 Scheduler，旧 `run_daily.sh` 已切到 MySQL Scheduler CLI；
- `scheduler_enabled=false` 时 CLI 明确拒绝运行，不触达旧 SQLite 或旧批量接口。
- 新增版本化脱敏样本 Schema 与只读验收命令，复用生产 Provider、Normalizer、Quality；
- 验收器强制清单和父体详情显式提供父级身份，并拒绝十个 Tool 中任何跨单元身份串号；
- 核心 Tool 空、关键字段未取得、跨父体证据和缺少任一 Tool 均返回非零退出码。

## 合同审计发现

### S4-B01 经营单元清单缺父级身份

- 分类：`design-risk` / 上线阻断；
- 文档事实：`erp_amazon_listing_query_page.records[]` 只声明子 `asin/sellerSku`，未声明
  `parentAsin/parentSellerSku`；
- 影响：无法可靠构造 `shop_id + parent_asin + parent_seller_sku` 经营单元业务键和父级取数绑定；
- 当前处理：Provider 严格拒绝缺字段记录，Scheduler 保持关闭；
- 可选变更 A：#30 每条补 `parentAsin`、`parentSellerSku`；
- 可选变更 B：改用已声明父级字段的 `erp_listing_follow_up_by_principal`，并接入非 SQLite 的
  权威负责人名册；两种方案都需验证重复父体、跨页和 Active/Inactive 样例。

### S4-B02 回传 Tool 与纯巡检合同冲突

- 分类：`design-risk` / S5 合同阻断；
- 文档事实：`submit_patrol_batch` 强制要求 `businessModel`、`proposal.details`、
  `recommendedBusinessModel` 和 `approvalLevel`；
- 影响：直接接入会迫使巡检 Agent 重新承担模式、建议和审批职责，违反最终方案；
- 当前处理：不接入巡检主链路；
- 外部最小变更：中控提供只接收 `InspectionResultEnvelope` 的 Result Sink Tool 和可验证 ACK，
  或把上述越界字段改为可选且不覆盖中控已有决策。

### S4-B03 Buy Box 归属仍无完整事实

- 分类：`evidence-gap`；
- 已接通：子体级主图/副图/A+、前台价格与促销、评论、合规、类目/属性及关键词排名；动态响应会校验子 ASIN，禁止父级详情错挂。
- 剩余缺口：`erp_asin_full_detail` 只返回当前 Buy Box 获胜卖家 ID，尚无“本店 Seller ID”对照，无法判断是否丢失。
- 当前处理：Buy Box 继续输出 `INSPECTION_POINT_COVERAGE_LIMITED`，不使用 `hasCart` 冒充归属判断。
- 外部最小变更：提供按店铺和站点稳定绑定的我方 Seller ID，并冻结身份字段合同。

## 验证结果

| 门禁 | 结果 |
|---|---|
| Python | 3.12.11 |
| MCP 样本、分页、字段、时效与错误专项 | 82/82 通过 |
| 全量离线 | 见 README 当前测试基线 |
| R2/R3/R7 回归 | 20/20 通过 |
| Scheduler 关闭门禁 | 通过，返回明确错误且不创建批次 |
| 测试 MCP | 真实只读验收完成：34 Tool 可发现，事实 9/10 成功；清单无权限、详情超时阻断 |
| MySQL Migration/Repository/S2/S3 兼容 | 已纳入真实 MySQL 全量回归 |
| MySQL 最终状态 | 8.0.46，revision `0003_feedback_inbox_decoupling`，当前保持 `INTERNAL_ONLY` |

## 启用条件

只有以下条件全部满足后才允许把 `feature_flags.scheduler_enabled` 改为 `true`：

1. #30 正式返回父级身份，或为已实现的 #9 Provider 配置非 SQLite 权威负责人/店铺/站点映射；
2. 脱敏样本通过 `scripts.validate_mcp_input_sample`，无阻断缺口和身份串号；
3. 10 个事实 Tool 在联调环境通过字段、空值、错误码和限流验证；
4. 真实样本 R2/R3 回归由业务 Owner 签认；
5. MySQL 测试库恢复连通并再次通过 S2/S3/S4 兼容与空库检查。
