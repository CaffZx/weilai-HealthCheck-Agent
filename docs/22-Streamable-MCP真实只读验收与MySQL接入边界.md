# Streamable MCP 真实只读验收与 MySQL 接入边界

> 验收日期：2026-08-04  
> 环境：用户提供的 `MCP_SYNC_GATEWAY` 测试网关  
> 操作边界：只执行初始化、`tools/list` 和查询类 Tool；未运行旧同步命令，未写 MySQL、SQLite 或外部系统  
> 结论：网关和项目相关查询可用；正式 V2 只能经脱敏、归一化后写 MySQL，暂不替代 AZListing 主事实合同

## 1. 服务与工具发现

- MCP 初始化和 API Key 鉴权成功；
- 服务名：`whp-starrocks-mcp`；
- 服务版本：`1.0.0`；
- 协议版本：`2025-06-18`；
- 共发现 53 个 Tool；
- 密钥只通过验收进程环境变量注入，未写入项目文件、配置或报告。

## 2. 项目相关查询验收

使用已核验的一个经营单元执行最小只读调用，只检查状态、耗时、记录数和字段形状，
不保存或展示业务字段值。

| 领域 | Tool | 结果 |
|---|---|---|
| 用户与店铺 | `sys_user_query`、`sprout_shop_query` | 成功 |
| 父体身份 | `parent_listing_detail` | 成功 |
| Listing | `listing_basic_info`、`az_extend_detail` | 成功 |
| 库存与竞品 | `parent_listing_stock_summary`、`direct_competitors` | 成功 |
| 销售与广告 | `product_sales`、`ad_product_report`、`sales_performance` | 成功 |
| 关键词 | `own_keyword_flow`、`flow_keywords` | 成功 |
| 广告补证 | `ad_search_term_report`、`ad_campaign_list` | 成功 |

14 个查询全部成功，单次响应约 0.75–7.55 秒。项目保留的 `sync_daily.MCPSession` 和
`sync_asin_owner.MCPSession` 也分别做了单次查询兼容验证，均成功；没有执行它们的落库入口。

## 3. 敏感字段既定合同与本地边界

真实响应字段集合显示：

- `sys_user_query` 包含密码相关字段；
- `sprout_shop_query` 包含密码、Access Token、Refresh Token、App Secret 等字段；
- `az_extend_detail` 也包含店铺鉴权字段。

验收过程没有输出这些字段的值。业务方已确认这是服务端既定接口设计，不要求 MCP 服务端停止
返回；V2 的责任是确保这些字段不进入归一化事实、内容哈希、MySQL、日志或错误上下文。

## 4. V2 MySQL 入库边界

正式 V2 不运行旧 `data.sync_daily`、`data.sync_asin_owner` 等 SQLite 同步入口。
这些同步器及一键同步、补数、证据、负责人、图片/关键词和广告覆写入口会在建立外部连接前
无条件拒绝；SQLite 盘点、备份、恢复和迁移工具也已永久关闭，唯一运行数据库为 MySQL。

V2 原始事实和归一化快照分别归档到：

- `t_patrol_raw_fact`；
- `t_patrol_fact_snapshot`。

本次增加两层安全边界：

1. `McpToolResult` 创建时递归删除密码、Token、Secret、API Key、Authorization、Cookie、
   Session 等字段，并掩码文本中的凭据；JSON 文本封套也先解析再递归清洗；
2. MySQL Repository 入库前再次执行同样的清洗；请求参数出现凭据字段时直接拒绝归档。

事实内容哈希基于清洗后的数据计算，MySQL 复用校验仍保持一致。错误文本和 warning 也会先
掩码再入库。

## 5. 双网关职责

- `AZLISTING_GATEWAY`：当前 V2 的首选事实源，默认地址为
  `http://mcp-gateway.example.com/mcp`；承担清单和 10 项正式事实 Tool，仍受清单权限、
  父级字段合同和 `erp_asin_full_detail` 超时阻断；客户端以 `AUDITED_ONLY` 白名单限制为这 11 个
  Tool，其他调用在网络请求前拒绝；
- `MCP_SYNC_GATEWAY`：已验证的补充经营查询源，可覆盖销售、广告、库存、Listing、竞品和关键词；
  默认 `enabled=false`，不得自动接管 AZListing 失败请求，也不得重新启用旧 SQLite 同步器。

正式纳入 V2 前仍需为每个补充 Tool 冻结：经营单元身份、字段类型、空值、时区、金额/比例口径、
分页、错误码、限流和 SLA，然后映射到 `FactCollector` 与 `FactNormalizer`。返回中的凭据字段按
既定规则丢弃，不纳入字段映射。

## 6. 补充事实显式映射状态

V2 已为本报告真实验证的 14 个 Streamable 查询 Tool 建立独立合同和业务字段白名单，代码位于：

- `clients/streamable_contract.py`：Tool、业务域、合同状态、允许字段；
- `facts/supplemental.py`：响应拆包、字段选择、源响应哈希和映射后哈希；
- `integrations/mcp_routing.py`：独立补充网关路由；
- `web/backend/deps.py`：`X-Api-Key` 客户端与禁用态适配器装配。

当前仅冻结已完成真实只读验证、且本轮正式接入的两个合同：

- `sales_performance`：历史补充源，已退役；月度目标改由 `erp_listing_monthly_goal` 提供；
- `az_extend_detail`：本店 `SELLING_PARTNER_ID` 来源；
- `listing_basic_info`：补充父级星级和评论数。

其余 12 个合同继续保持 `VERIFIED_DRAFT`。运行配置为：

- `supplemental_mcp.enabled=true`；
- `supplemental_mcp.contract_policy=FROZEN_ONLY`；
- `supplemental_mcp.enabled_tools=[listing_basic_info]`；
- `INTERNAL_ONLY` 允许这两个只读补充事实，但仍禁止 Scheduler、Result Delivery、
  Control Center Delivery 和 Feedback；
- 补充事实经白名单脱敏后进入 `FactCollector`、Raw Fact 归档与统一快照血缘，
  不作为 AZListing 工具失败时的自动同名替代。

因此当前只启用两个明确用途的补充事实，不会在 AZListing 失败时自动切换网关。

产品主图不取 `az_extend_detail.IMAGE_URL`，该字段是店铺 Logo。V2 从前台详情中的真实产品图
更新 `t_patrol_product_image`；只有非空图片才覆盖缓存，采集失败或新品暂时无图时继续使用
同一 `shopId + parentAsin + parentSellerSku` 最近一次成功图片。独立
`pangolinfo_api_sync_Extract` 当前在两个网关均不可用，因此不加入同步巡检阻塞主流程。

## 7. 当前结论

同步网关的连接、鉴权、工具发现、核心查询和既有客户端兼容性均通过。敏感字段清洗已经在 V2
客户端与 MySQL Repository 落地并有回归测试；服务端继续按既定合同返回。双网关外部合同未
冻结前，继续保持 `INTERNAL_ONLY`、`scheduler_enabled=false`。

当前基线为 375 项：368 项离线通过，7 项真实 MySQL 测试默认跳过；Ruff 与 compileall 通过。
