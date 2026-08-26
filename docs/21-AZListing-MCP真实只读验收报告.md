# AZListing MCP 真实只读验收报告

> 验收日期：2026-08-04  
> 环境：用户提供的 AZListing MCP 测试网关  
> 操作边界：只执行初始化、`tools/list` 和查询类 Tool；未调用提交、修改或删除类 Tool  
> 结论：事实查询 9/10 可用；清单权限和父体详情超时仍阻断自动调度

> 2026-08-04 补充：用户已提供完整 33 Tool 清单和 `outputFields()` 字段字典。本文不再把
> “缺少 Tool 字段字典”列为阻断；下述阻断专指运行权限、经营单元枚举和真实响应验收。

## 1. 服务与工具发现

- MCP 初始化成功，鉴权可用；
- 服务名：`whp-azlisting-mcp-server`；
- 服务版本：`1.0.0`；
- 协议版本：`2025-06-18`；
- 共发现 34 个 Tool；
- 巡检当前依赖的清单 Tool 和 10 个事实 Tool 全部存在。

密钥只通过进程环境变量注入，未写入项目文件、配置、报告或测试数据。

## 2. 清单 Tool 验收

`erp_amazon_listing_query_page` 的真实 Tool Schema 只声明：

- `shopId`、`shopAccount`、`siteCode`；
- 子级 `asin`、`sellerSku`；
- 状态、产品信息和分页字段。

Schema 没有声明 `parentAsin`、`parentSellerSku`。使用当前 API Key 做最小只读调用时，网关
返回 `无权调用工具: erp_amazon_listing_query_page`。因此无法进一步核对实际记录是否包含
Schema 未声明的父级字段，也无法用该 Key 验证分页稳定性。

完整清单另确认 `erp_listing_follow_up_by_principal` 的分页记录包含 `shopAccount`、子 ASIN/SKU、
`parentAsin`、`parentSellerSku` 和状态，最大单页 200；但它要求精确 `principalName`。因此当前
API Key 不能支撑默认 #30 Scheduler；#9 备用 Provider 已实现，但启用前需注入并确认非 SQLite
的权威负责人/店铺/站点映射。不能用子 ASIN/SKU 冒充父级身份，也不能让旧 SQLite 成为 V2 运行权威。

## 3. 十个事实 Tool 验收

使用已审计确认的一个经营单元做单次只读查询，只输出行数和字段形状，不保存或展示业务值：

| Tool | 结果 | 关键形状 |
|---|---|---|
| `erp_listing_product_info` | 成功 | 子体、父 ASIN/父 SKU、内容和变体字段 |
| `erp_listing_gross_profit_history` | 成功 | `summary + detail` |
| `erp_listing_business_report` | 成功 | `summary + records` |
| `erp_listing_natural_advert_flow` | 成功 | 汇总/子体流量、广告字段 |
| `erp_listing_stock_alert` | 成功 | 可售、入库、预留、在途和身份字段 |
| `erp_listing_unit_profit_analysis` | 成功 | 售价、佣金、成本、FBA 费用、推广成本比 |
| `erp_listing_inventory_cost_analysis` | 成功 | 父 ASIN、月份、子体与汇总 |
| `erp_listing_advert_agent_config` | 成功 | 父身份、目标 ACOS、预算和策略字段 |
| `erp_listing_refund_rate` | 成功 | `summary + children` |
| `erp_asin_full_detail` | 失败 | 父 ASIN和子 ASIN各一次，60 秒均超时 |

生产归一化与质量逻辑可取得子体、销量、库存等关键事实，但详情 Tool 超时会产生阻断缺口
`MCP_LISTING_IDENTITY_UNAVAILABLE`。当前不能把 9/10 成功视为完整事实合同通过。

## 4. 客户端兼容修复

真实网关暴露了三项此前离线样本未覆盖的协议差异，现已修复并增加回归测试：

1. Tool 结果可能再包一层 MCP 封套；客户端现在有限深度递归解析；
2. SDK 序列化错误标志为 `is_error`，客户端同时兼容 `isError`；
3. SDK 会在会话退出时用 `ExceptionGroup` 包住业务错误，客户端现在解包保留稳定错误码。

新增 `MCP_PERMISSION_DENIED`：HTTP 403、不可重试。清单权限错误现在一次即停，不再误判为
传输异常重复调用；缺少必需参数按 `MCP_CONTRACT_INVALID` 处理。

## 5. 当前门禁

补充可行路径：全量发现不必依赖 #30 单个接口。可先通过 `sys_user_query` 和
`sprout_shop_query` 获取有效运营负责人及店铺站点，再按负责人×店铺调用
`erp_listing_follow_up_by_principal`，该接口直接返回父 ASIN/父 Seller SKU。巡检侧已实现
`PRINCIPAL_DIRECTORY` 组合 Provider，并继续拒绝任何缺父级字段的记录。

进入真实 E2E 或 SHADOW 前，MCP 与巡检双方至少需要：

1. 为联调 Key 开通 `erp_amazon_listing_query_page` 只读权限；
2. 冻结经营单元枚举策略：#30 增加可信父字段，或 #9 配套权威负责人名册；
3. 排查 `erp_asin_full_detail` 父/子 ASIN均超过 60 秒的问题，并给出 SLA；
4. 提供分页稳定、Inactive、重复父体和身份冲突样例；
5. 以已提供字段字典为基线，验证 10 个事实 Tool 的真实空值、时区、金额与比例口径并签字。

上述问题解除前继续保持 `INTERNAL_ONLY`，`scheduler_enabled=false`，不得开启真实自动巡检。

## 6. 回归状态

- MCP 客户端、事实、清单与样本专项通过；
- 后续已升级为 `amazon_ops.azlisting_mcp_input.internal.v2` 机器合同，加入 #9 备用 Provider、运行时身份/核心字段校验和
  外部未冻结时的 Scheduler 预检门禁；详见 `docs/23-AZListing-MCP内部冻结基线与外部门禁.md`；
- 全量测试计数以 README 当前基线为准；
- Ruff、compileall、合同导出同步与凭据扫描通过；
