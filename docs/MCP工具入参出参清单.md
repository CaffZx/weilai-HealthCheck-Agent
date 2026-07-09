# MCP 工具入参 / 出参清单

> 覆盖两个 MCP 服务器共 71 个工具。入参来自工具 schema，出参为逐工具真实调用实测观察（🔒=无权限 ⚠️=报错/空 ✅=可用且已验证）。

## 目录
- [服务器一：azlisting-mcpserver（Listing/库存/流量/竞品，20 工具）](#服务器一azlisting-mcpserver)
- [服务器二：streamable-mcpserver（广告/销售/竞品/多平台，51 工具）](#服务器二streamable-mcpserver)

## 服务器一：azlisting-mcpserver

- **网关地址**：`http://mcp-gateway.example.com/mcp`
- **认证头**：`X-Api-Key: sk-starsrock-...（或 Authorization: Bearer ...）`
- **工具数**：20
- 实测：每日销量趋势/自然流量/滞销仓租/库存预警/月度目标 可用；链接状态(linkStatus)声明有但实测空。

### `erp_asin_full_detail`

Fetch full ASIN data including link status, inventory, price, seller, BuyBox, Amazon's Choice, main image, A+ content and reviews.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | ASIN, for example B0C1234567 |
| `siteCode` | 字符串 | ✅ | Site code, for example US/UK/DE/FR/IT/ES/JP/CA |
| `zipCode` | 字符串 |  | Zip code |
| `includeReviews` | 字符串 |  | Whether to include deep reviews, true/false |
| `reviewMaxStar` | 字符串 |  | Review max star |
| `reviewWithinDays` | 字符串 |  | Review within days |

**出参**：data[{ asin, siteCode, linkStatus{⚠️实测空}, images{⚠️实测空}, aplus, bonus, reviews{lowStarRecentCount, lowStarRecentList} }] —— 声明有链接状态/BuyBox但实测未落数据

---

### `erp_listing_advert_agent_config`

Advert agent strategy configuration. Requires shopAccount + parentAsin. parentSellerSku is optional.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `parentAsin` | 字符串 | ✅ | Parent ASIN |
| `parentSellerSku` | 字符串 |  | Parent seller SKU |

**出参**：广告代理策略配置：目标ACOS / 日预算 / 产品阶段 / 价格定位 / 淡旺季决策

---

### `erp_listing_competitor_price_monitor`

竞品价格监控：查询竞品的 Coupon、折扣和活动信息。必填店铺账号和竞品 ASIN。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `siteCode` | 字符串 | ✅ | 站点编码 |
| `competitorAsin` | 字符串 | ✅ | 竞品 ASIN |

**出参**：竞品实时价格 / Coupon / 折扣活动

---

### `erp_listing_competitor_variant_analysis`

Competitor variant analysis for a company ASIN. Requires shopAccount + asin. competitorAsin is optional.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `asin` | 字符串 | ✅ | Company ASIN |
| `competitorAsin` | 字符串 |  | Competitor ASIN |

**出参**：竞品变体列表 + 每月各变体销量占比

---

### `erp_listing_inventory_cost_analysis`

库存费用分析 —— 查询超龄仓租费用。必填 shopAccount + parentAsin + parentSeller。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | 店铺账号 |
| `parentAsin` | 字符串 | ✅ | 父 ASIN |
| `parentSeller` | 字符串 | ✅ | 父卖家 SKU |

**出参**：data[{ parentAsin, reportMonth, children[{ asin, fnSku, sellerSku, longTermStorageFees[] }] }] —— 超龄仓租明细

---

### `erp_listing_keyword_rank_tracker`

关键词排名追踪 —— 查 t_az_precise_keyword_library 表，获取关键词周排名/搜索量。必填 keyword + siteCode，不传时间返回本周+上周，传了返回范围内全部周数据。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | 字符串 | ✅ | 搜索关键词 |
| `siteCode` | 字符串 | ✅ | 站点编码，如 US |
| `startDate` | 字符串 |  | 开始日期 yyyy-MM-dd |
| `endDate` | 字符串 |  | 结束日期 yyyy-MM-dd |

**出参**：关键词周排名 / 搜索量（t_az_precise_keyword_library）

---

### `erp_listing_long_term_inventory_fee_trend`

Long-term inventory fee trend for a listing. Supports parent mode (shopAccount + parentAsin + parentSeller) or child mode (shopAccount + asin + sellerSku). recentDays means the most recent N days from today and defaults to 7.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `parentAsin` | 字符串 |  | Parent ASIN for parent mode |
| `parentSeller` | 字符串 |  | Parent seller SKU for parent mode |
| `asin` | 字符串 |  | Child ASIN for child mode |
| `sellerSku` | 字符串 |  | Child seller SKU for child mode |
| `recentDays` | 字符串 |  | Most recent N days, defaults to 7 |

**出参**：长期仓储费趋势（父/子模式）

---

### `erp_listing_monthly_goal`

Monthly goal for the current month and the next 3 months. Requires shopAccount + parentAsin.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `parentAsin` | 字符串 | ✅ | Parent ASIN |

**出参**：data（当月+未来3月逐月目标：排名/单量/系数/负责人）

---

### `erp_listing_natural_advert_flow`

Natural/ad flow and ad efficiency. Returns summary and child-ASIN detail rows for a parent listing or a child listing. Requires shopAccount, startDate, endDate, and one of (parentAsin + parentSeller) or (asin + sellerSku).

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `parentAsin` | 字符串 |  | Parent ASIN for parent mode |
| `parentSeller` | 字符串 |  | Parent seller SKU for parent mode |
| `asin` | 字符串 |  | Child ASIN for child mode |
| `sellerSku` | 字符串 |  | Child seller SKU for child mode |
| `startDate` | 字符串 | ✅ | Start date in yyyy-MM-dd |
| `endDate` | 字符串 | ✅ | End date in yyyy-MM-dd |

**出参**：data[{ asin, sellerSku, parentAsin, isSummary, adOrderNum, totalOrderNum, naturalOrderNum, adSaleAmountCny, adCostAmountCny, totalSaleAmountCny, tacos, tacosPercent, adClick, adImpressions, adSaleNum }] —— ✅ 自然/广告订单拆分

---

### `erp_listing_operation_overview`

Online listing operation overview: aggregate natural order data. Supports parent mode (shopAccount + parentAsin + parentSeller) or child mode (shopAccount + asin + sellerSku). startDate/endDate are required for the order statistics.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `parentAsin` | 字符串 |  | Parent ASIN for parent mode |
| `parentSeller` | 字符串 |  | Parent seller SKU for parent mode |
| `asin` | 字符串 |  | Child ASIN for child mode |
| `sellerSku` | 字符串 |  | Child seller SKU for child mode |
| `startDate` | 字符串 | ✅ | Start date in yyyy-MM-dd |
| `endDate` | 字符串 | ✅ | End date in yyyy-MM-dd |

**出参**：自然订单占比 + TACOS 运营总览（父/子模式）

---

### `erp_listing_price_promotion_analysis`

价格促销分析 —— 实时爬取亚马逊商品详情，获取价格/Coupon/LD/BD/Prime折扣。必填 asin（子 ASIN）+ siteCode（如 US）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | 子 ASIN |
| `siteCode` | 字符串 | ✅ | 站点编码，例如 US |

**出参**：实时爬：售价 / Coupon / LD / BD / Prime折扣 / 星级 / BSR

---

### `erp_listing_price_trend`

Price trend for ASINs in a date range. Requires shopAccount, asinList(JSON array), startDate and endDate.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `asinListJson` | 字符串 | ✅ | ASIN list JSON array, for example ["B0C1234567","B0C7654321"] |
| `startDate` | 字符串 | ✅ | Start date in yyyy-MM-dd |
| `endDate` | 字符串 | ✅ | End date in yyyy-MM-dd |

**出参**：批量ASIN(最多50) 区间历史价格趋势

---

### `erp_listing_product_detail`

Query product detail by ASIN and siteCode. shopAccount is optional, but required for some shop-scoped metrics.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | ASIN, for example B0C1234567 |
| `siteCode` | 字符串 | ✅ | Site code, for example US/UK/DE/JP |
| `shopAccount` | 字符串 |  | Shop account |

**出参**：（未实测 / 待补充）

---

### `erp_listing_product_info`

Query product information by parent ASIN, including parent-child relations, fineness, variant theme, target stars, bullet points, short-term goals and color labels.

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `paramsJson` | 字符串 | ✅ | JSON object string. Required fields: shopAccount, parentAsin, parentSellerSku |

**出参**：父子体关系 / 变体主题 / 目标星级 / 五点描述 / 短期目标 / 颜色标签(主要色/次要色/长尾色)（需 shopAccount+parentSellerSku）

---

### `erp_listing_quality_risk_analysis`

质量风险分析 —— 查询退货记录（退货原因、客户评论）。必填 shopAccount + sellerSKU + asin。可选 startDate/endDate，不传展示全部。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | 店铺账号 |
| `sellerSku` | 字符串 | ✅ | 卖家 SKU |
| `asin` | 字符串 | ✅ | 子 ASIN |
| `startDate` | 字符串 |  | 开始日期 yyyy-MM-dd |
| `endDate` | 字符串 |  | 结束日期 yyyy-MM-dd |

**出参**：退货记录：退货原因 + 客户评论

---

### `erp_listing_review_analysis`

评论分析 —— 固定抓取亚马逊 all_stars 前 5 页评论，按实际星级分组返回。必填 asin（子 ASIN）+ siteCode（如 US）。保留兼容入参 page/days，但当前固定抓 5 页。每评论带 reviewType：negative/neutral/positive。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | 子 ASIN |
| `siteCode` | 字符串 | ✅ | 站点编码，例如 US |
| `page` | 整数 |  | 评论页码，默认 1 |
| `days` | 整数 |  | 仅返回最近 N 天内的评论 |

**出参**：5页评论按星级分组，每评论带 reviewType(negative/neutral/positive) —— ⚠️实时爬较慢易超时

---

### `erp_listing_sales_price_trend`

销量价格趋势 —— 查询子 ASIN 的每日价格和销量趋势。必填 shopAccount + asin（子ASIN）+ sellerSku + startDate + endDate（yyyy-MM-dd，最多30天）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | 店铺账号 |
| `asin` | 字符串 | ✅ | 子 ASIN |
| `sellerSku` | 字符串 | ✅ | 子卖家 SKU |
| `startDate` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `endDate` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：data[{ asin, sellerSku, shopAccount, recordDate, price, sales }] —— ✅ 每日一行，一次取整段（最多30天）

---

### `erp_listing_stock_alert`

库存预警 —— 查询在线列表的库存数据（可售/入库/预留/不可售/调查中）,仓库库存（中转/直发等）+ 备货周期/物流时效 + 预计缺货日/缺货天数。 支持父在线列表（shopAccount+parentAsin+parentSeller） 或子在线列表（shopAccount+asin+sellerSku），二选一必填

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | Shop account from t_shop.account |
| `parentAsin` | 字符串 |  | Parent ASIN for parent mode |
| `parentSeller` | 字符串 |  | Parent seller SKU for parent mode |
| `asin` | 字符串 |  | Child ASIN for child mode |
| `sellerSku` | 字符串 |  | Child seller SKU for child mode |

**出参**：data[{ canSaleNum, inStockNum, inStockWorkingNum, inStockReceivingNum, inStockShippedNum, reserveNum, noSaleNum, investigationNum, transferInStock, directShipStock, totalStock, purchaseOnWay, waitQuality, waitPacked, waitShelve, waitPicking, ...预计缺货日/缺货天数 }] —— ✅ 库存全维度

---

### `erp_listing_task_management`

任务管理 —— 查询运营任务及跟进状态。必填 parentAsin，可选 taskStatus 筛选。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parentAsin` | 字符串 | ✅ | 父 ASIN |
| `taskStatus` | 字符串 |  | 任务状态筛选（可选） |

**出参**：运营任务：分配 / 跟进状态 / 截止时间

---

### `erp_listing_unit_profit_analysis`

单件费用分析 —— 查询每个 SKU 的售价、佣金、成本、推广成本比。必填 shopAccount + parentAsin + parentSeller。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | 店铺账号 |
| `parentAsin` | 字符串 | ✅ | 父 ASIN |
| `parentSeller` | 字符串 | ✅ | 父卖家 SKU |

**出参**：按SKU：售价 / 佣金 / 成本 / FBA费 / 推广成本比

---

## 服务器二：streamable-mcpserver

- **网关地址**：`http://mcp-gateway.example.com/mcp`
- **认证头**：`X-Api-Key: sk-starsrock-...`
- **工具数**：51
- 实测：广告全套/销售/Listing/库存汇总/竞品清单 可用；ERP/系统/SHEIN/TEMU 类无权限。

### `az_cw_asin_prod_info_query`

分页查询竞品ASIN产品信息（爬虫数据）。支持按ASIN、站点、父ASIN、大类目、配送模式等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 |  | ASIN（精确匹配） |
| `siteCode` | 字符串 |  | 站点代码，如 US/UK/DE |
| `pasin` | 字符串 |  | 父ASIN |
| `bigCategory` | 字符串 |  | 大类目 |
| `expressMode` | 字符串 |  | 配送模式 (Amazon/FBM) |
| `buyBoxCountry` | 字符串 |  | BuyBox国家 |
| `startEstimatedMonthSale` | 整数 |  | 月销量起始 |
| `endEstimatedMonthSale` | 整数 |  | 月销量截止 |
| `pageNo` | 整数 |  | 页码，默认1 |
| `pageSize` | 整数 |  | 每页条数，默认10 |

**出参**：⚠️ HTTP 400（竞品爬虫库，参数待确认）

---

### `consumable_library_query`

分页查询耗材库数据。支持按编码模糊、名称模糊、编码精确、是否必损、是否默认耗材等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `codeLike` | 字符串 |  | 耗材编码模糊 |
| `nameLike` | 字符串 |  | 耗材名称模糊 |
| `code` | 字符串 |  | 耗材编码（精确匹配） |
| `name` | 字符串 |  | 耗材名称（精确匹配） |
| `defaultConsumable` | 字符串 |  | 是否默认耗材（yes/no） |
| `consumableLoss` | 字符串 |  | 是否必损耗材（yes/no） |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 当前 apiKey 无权限

---

### `erp_product_query`

查询数仓产品信息。按父SKU列表(parentSkuList)或子SKU列表(qryViewChildSkuList)查询产品。两个参数至少传一个；qryViewChildSkuList 单个值时模糊匹配，多个值时精确匹配。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parentSkuList` | 字符串 | ✅ | 父SKU列表，JSON 数组格式，如 ["PSKU001","PSKU002"]；与 qryViewChildSkuList 至少传一个 |
| `qryViewChildSkuList` | 字符串 | ✅ | 子SKU列表，JSON 数组格式，如 ["CSKU001"]；单个模糊、多个精确；与 parentSkuList 至少传一个 |

**出参**：🔒 当前 apiKey 无权限

---

### `operate_expenses_query`

分页查询仓储操作费配置数据。支持按编码模糊、名称模糊、编码精确、名称精确等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `codeLike` | 字符串 |  | 操作费编码模糊 |
| `nameLike` | 字符串 |  | 操作费名称模糊 |
| `code` | 字符串 |  | 操作费编码（精确匹配） |
| `name` | 字符串 |  | 操作费名称（精确匹配） |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 当前 apiKey 无权限

---

### `pangolinfo_api_sync_Extract`

通过外部 pangolinfo_api 实时爬虫api。需提供 asin,url,parserName,siteCode参数进行数据实时提取

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | Amazon ASIN，如 B0DQLB8WWC,若传入ASIN 则 parserName = 'amzProductDetail',若url有值 则asin 可不传 |
| `url` | 字符串 | ✅ | 亚马逊提取连接,比如关键词提取url:https://www.amazon.com/dp/B0C6QBC9YJ 或者 https://www.amazon.com/s?k=bath+toys+for+toddlers+1-3&page=1 |
| `parserName` | 字符串 | ✅ | 解析器名称  |
| `siteCode` | 字符串 | ✅ | 站点代码，如 Amazon_US Amazon_UK Amazon_DE |

**出参**：⚠️ 需传 url 参数（实时爬虫）

---

### `seller_sprite_competitor_info`

查询卖家精灵竞品信息。需提供 ASIN 与市场站点（支持 Amazon_US / US 这类站点格式，调用时会统一为短码）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | Amazon ASIN，如 B0DQLB8WWC |
| `market` | 字符串 | ✅ | 市场站点代码，如 Amazon_US、US、Amazon_DE、DE |

**出参**：⚠️ 外部服务需登录验证码，HTTP 500

---

### `seller_sprite_keyword_reverse`

查询卖家精灵关键词反查数据。需提供 ASIN 与市场站点（支持 Amazon_US / US 这类站点格式，调用时会统一为短码）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | Amazon ASIN，如 B0DQLB8WWC |
| `market` | 字符串 | ✅ | 市场站点代码，如 Amazon_US、US、Amazon_DE、DE |
| `page` | 整数 |  | 分页页码，默认 1 |
| `page_size` | 整数 |  | 分页大小，默认 100 |

**出参**：data（卖家精灵关键词反查）

---

### `seller_sprite_price_trend`

查询卖家精灵价格趋势数据。需提供 ASIN 与市场站点（支持 Amazon_US / US 这类站点格式，调用时会统一为短码）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | Amazon ASIN，如 B0DQLB8WWC |
| `market` | 字符串 | ✅ | 市场站点代码，如 Amazon_US、US、Amazon_DE、DE |
| `page` | 整数 |  | 分页页码，默认 1 |
| `page_size` | 整数 |  | 分页大小，默认 100 |

**出参**：⚠️ 外部服务需登录验证码，HTTP 500

---

### `shein_map_relation_query`

分页查询SHEIN-亚马逊商品映射关系数据。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `azFnSkuLike` | 字符串 |  | 亚马逊FNSKU模糊 |
| `sheinSkuLike` | 字符串 |  | SHEIN SKU模糊 |
| `originSkuLike` | 字符串 |  | 原始SKU模糊 |
| `startCreateTime` | 字符串 |  | 创建时间开始，格式 yyyy-MM-dd HH:mm:ss |
| `endCreateTime` | 字符串 |  | 创建时间结束，格式 yyyy-MM-dd HH:mm:ss |
| `startTakeOverTime` | 字符串 |  | 接管时间开始，格式 yyyy-MM-dd HH:mm:ss |
| `endTakeOverTime` | 字符串 |  | 接管时间结束，格式 yyyy-MM-dd HH:mm:ss |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 无权限（SHEIN 平台）

---

### `shein_platform_goods_stock_query`

分页查询SHEIN平台产品库存数据。支持按SPU、SKC、SKU、店铺等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `spuName` | 字符串 |  | SPU名称（精确匹配） |
| `skcName` | 字符串 |  | SKC名称（精确匹配） |
| `skuCode` | 字符串 |  | SKU编码（精确匹配） |
| `shopId` | 字符串 |  | 店铺ID |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 无权限（SHEIN）

---

### `shein_skc_query`

分页查询SHEIN在线列表SKC数据。默认会查询关联SKU。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `spuName` | 字符串 |  | SPU名称（精确匹配） |
| `skcName` | 字符串 |  | SKC名称（精确匹配） |
| `shopId` | 字符串 |  | 店铺ID |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 无权限（SHEIN）

---

### `shein_sku_query`

分页查询SHEIN在线列表SKU数据。默认会查询亚马逊FBA库存、SKU扩展信息、销量、海外仓库存。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `spuName` | 字符串 |  | SPU名称（精确匹配） |
| `skcName` | 字符串 |  | SKC名称（精确匹配） |
| `skuCode` | 字符串 |  | SKU编码（精确匹配） |
| `originSku` | 字符串 |  | 仓库SKU（精确匹配） |
| `supplierSku` | 字符串 |  | 卖家SKU（精确匹配） |
| `shopId` | 字符串 |  | 店铺ID（精确匹配） |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 无权限（SHEIN）

---

### `shein_spu_query`

分页查询SHEIN在线列表SPU数据。默认会查询子SKU、销量、库存数据。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `spuNameLike` | 字符串 |  | SPU名称模糊 |
| `skcNameLike` | 字符串 |  | SKC名称模糊 |
| `skuCodeLike` | 字符串 |  | SKU编码模糊 |
| `originSkuLike` | 字符串 |  | 仓库SKU模糊 |
| `supplierCodeLike` | 字符串 |  | 供货方号模糊 |
| `skcStatus` | 字符串 |  | SKC状态（已上架/已下架/已售罄） |
| `siteAbbr` | 字符串 |  | 上架站点缩写 |
| `startFirstShelfTime` | 字符串 |  | 首次上架时间开始，格式 yyyy-MM-dd HH:mm:ss |
| `endFirstShelfTime` | 字符串 |  | 首次上架时间结束，格式 yyyy-MM-dd HH:mm:ss |
| `azShopId` | 字符串 |  | 亚马逊店铺ID |
| `azFnSkuLike` | 字符串 |  | 亚马逊FNSKU模糊 |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认100，最大100 |

**出参**：🔒 无权限（SHEIN）

---

### `shein_spu_principal_query`

按店铺账号查询SHEIN在线列表SPU及负责人名称。返回每个SPU的名称和负责人列表。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | 店铺账号 |

**出参**：🔒 无权限（SHEIN）

---

### `sprout_shop_query`

分页查询豆芽店铺数据。支持按账号模糊、账号精确、店铺名称、平台、是否废弃、紫鸟账号等条件筛选。默认查询Amazon平台店铺。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `accountLike` | 字符串 |  | 店铺账号模糊 |
| `account` | 字符串 |  | 店铺账号（精确匹配） |
| `name` | 字符串 |  | 店铺名称（精确匹配） |
| `platformCode` | 字符串 |  | 平台代码，如 Amazon/SHEIN/TEMU/TikTokShop；不传默认查Amazon |
| `superBrowserAccount` | 字符串 |  | 紫鸟浏览器账号模糊 |
| `waste` | 字符串 |  | 是否废弃（yes/no），不传默认排除已废弃 |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认10，最大10 |

**出参**：🔒 当前 apiKey 无权限

---

### `sys_user_query`

分页查询系统用户数据。支持按用户名模糊、账号模糊、用户名精确、账号精确、用户状态等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `userNameLike` | 字符串 |  | 用户名模糊 |
| `userAccountLike` | 字符串 |  | 账号模糊 |
| `userName` | 字符串 |  | 用户名（精确匹配） |
| `userAccount` | 字符串 |  | 账号（精确匹配） |
| `userState` | 整数 |  | 用户状态（1=启用/0=停用） |
| `pageNo` | 整数 |  | 页码，从1开始，不传默认1 |
| `pageSize` | 整数 |  | 每页条数，不传默认10，最大10 |

**出参**：🔒 当前 apiKey 无权限

---

### `ad_campaign_basic_info`

查询广告活动基础信息：广告活动名称、预算、状态、创建日期、活动上线天数。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `campaign_name` | 字符串 |  | 广告活动名称（与 campaign_name_list 二选一） |
| `campaign_name_list` | 字符串 |  | 广告活动名称列表，最多传20个，逗号分隔（与 campaign_name 二选一） |

**出参**：广告活动名称 / 广告活动预算 / 状态 / 广告活动创建日期 / 关键词 / 关键词BID / 头部位置加价比例 / 其他位置加价比例

---

### `ad_campaign_basic_info_v2`

查询广告活动基础信息V2：按活动ID（索引字段）查询，返回名称、预算、状态、关键词、BID、位置加价、创建日期、上线天数。请配合 ad_campaign_list 使用（先用 list 拿 id，再用此工具批量查详情）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `campaign_id` | 字符串 |  | 广告活动ID（与 campaign_id_list 二选一） |
| `campaign_id_list` | 字符串 |  | 广告活动ID列表，最多传50个，逗号分隔（与 campaign_id 二选一） |

**出参**：同上 + 关键词匹配类型（按活动ID查）

---

### `ad_campaign_list`

查询广告活动列表（仅活动名+活动ID）：快速获取启用的广告活动名称和ID，不含关键词/子ASIN详情。推荐 agent 发现阶段优先使用。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：广告活动名称 / 广告活动id

---

### `ad_campaign_placement_report`

查询广告活动广告位报告：按广告活动名称与广告位分组统计 CTR/CPC/CVR/ACOS。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `campaign_name` | 字符串 | ✅ | 广告活动名称 |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：按广告位：CTR / CPC / CVR / ACOS / 花费 / 销售额 / 广告订单量 / 销售数量 / 币种

---

### `ad_campaign_product_keyword_list`

查询广告活动产品关键词列表：广告活动名称、广告活动id、关键词、关键词id、匹配类型、子ASIN、子SKU等。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：子SIN / 子卖家KU / 关键词 / 关键词ID / 关键词匹配类型 / 广告活动名称 / 广告活动D

---

### `ad_campaign_product_report`

查询广告活动产品报告：按广告活动名称聚合曝光量、点击量、CTR、CPC、CVR、销售额、ACOS、日均花费等指标。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `campaign_name` | 字符串 | ✅ | 广告活动名称 |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：CTR / CPC / CVR / ACOS / 花费 / 日均花费 / 销售额 / 广告订单量 / 币种

---

### `ad_campaign_search_term_report`

查询广告活动搜索词报告：按广告活动名称与搜索词分组展示各关键词的广告效果数据。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `campaign_name` | 字符串 | ✅ | 广告活动名称 |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：（实测空，需该活动有搜索词数据）

---

### `ad_optimization`

广告优化模板：按广告活动名称、关键词、店铺账号与时间范围，返回 CPC、CVR、ACOS、CTR、ROAS、自然/广告订单占比、精准/广泛广告花费、精准/广泛广告ACOS、精准/广泛广告CPC。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `campaign_name` | 字符串 | ✅ | 广告活动名称 |
| `keyword` | 字符串 | ✅ | 关键词 |
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：CTR / CVR / ACOS / CPC / ROAS / 广告订单占比 / 精准广告花费 / 精准广告ACOS / 精准广告CPC / 广泛广告花费 / 广泛广告ACOS / 广泛广告CPC

---

### `ad_placement_report`

查询广告位报告：按广告位(Top of Search/Product Pages等)分组统计CTR/CPC/CVR/ACOS。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：按广告位：CTR / CPC / CVR / ACOS / 花费 / 销售额 / 广告订单量 / 销售数量 / 币种

---

### `ad_product_report`

查询广告产品报告：曝光量、点击量、CTR、CPC、CVR、销售额、ACOS等指标。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：CTR / CPC / CVR / ACOS / 花费 / 销售额 / 广告订单量 / 销售数量 / 点击量 / 曝光量 / 币种

---

### `ad_search_term_report`

查询广告搜索词报告：按搜索词分组展示各关键词的广告效果数据。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：搜索词 / CTR / CPC / CVR / ACOS / 花费 / 销售额 / 广告订单量 / 销售数量 / 点击量

---

### `advert_all_campaign_list`

查询广告活动全量列表：返回跟卖产品下所有启用广告活动的名称、活动ID、所属广告组合ID及组合名称（含精准测试组、精准主力组、自动广泛组）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：广告活动名称 / 广告活动id / 广告组合名称 / 广告组合id

---

### `az_extend_detail`

查询亚马逊在线列表扩展信息。按店铺账号、ASIN、SellerSKU精确匹配，返回单条记录的全部扩展字段。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 |
| `asin` | 字符串 | ✅ | ASIN |
| `seller_sku` | 字符串 | ✅ | Seller SKU |

**出参**：120个扩展字段，含：SEASONALITY(淡旺季) / PRODUCT_GRADE(等级) / TARGET_STAR_RATE(目标评分) / STAR_LEVEL(当前星级) / STOCK_INVENTORY(库存) / ASIN_START_SALE_DATE(上架日) / REFUND_RATE / COMMENT_NUM / TARGET_GROSS_PROFIT / AUTH_STATE 等（无 BuyBox/下架状态）

---

### `direct_competitors`

查询AI识别的直接竞品信息：包含竞品标题、价格、星级、评论数、类目排名、品牌、颜色、尺码等详细信息。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：竞品 / 父ASIN / 品牌 / 星级 / ratings数 / 评论数 / 颜色 / 面料 / 变体数量 / 末级类目排名

---

### `flow_keywords`

按Amazon站点(US/UK/DE)查询在线列表的流量关键词及其搜索量、搜索排名。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site_code` | 字符串 | ✅ | 站点代码: Amazon_US 或 Amazon_UK 或 Amazon_DE |
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：关键词 / 搜索量 / 搜索排名

---

### `keyword_child_asins`

查询指定关键词下自己产品的子ASIN自然排名和广告排名情况。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | 字符串 | ✅ | 搜索关键词 |
| `site_code` | 字符串 | ✅ | 站点代码: Amazon_US 或 Amazon_UK 或 Amazon_DE |
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：（需该产品在该关键词下有排名数据，测试产品多为空）

---

### `keyword_competitor_flow`

竞品流量关键词查询：按关键词、竞品ASIN、站点返回自然排名、产品/广告流量占比、潜力值、品牌搜索排名及关键词搜索量。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | 字符串 | ✅ | 搜索关键词 |
| `asin` | 字符串 | ✅ | 竞品 ASIN |
| `site_code` | 字符串 | ✅ | 站点代码: Amazon_US / Amazon_UK / Amazon_DE，也支持 US/DE 简写 |

**出参**：（同上）

---

### `keyword_competitors`

查询指定关键词下的竞品ASIN，包含自然排位排名、广告排位排名、价格、标题等信息。用于竞品监控。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | 字符串 | ✅ | 搜索关键词 |
| `site_code` | 字符串 | ✅ | 站点代码: Amazon_US 或 Amazon_UK 或 Amazon_DE |
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：（同上，多为空）

---

### `listing_basic_info`

Listing基础信息：按店铺账号查询跟卖 Listing 的标题、售价、评论数/星级、变体数量、评论内容、16周/32周退款率、类目退换货率、大类/小类排名、类目转化率、链接转化率。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `parent_asin` | 字符串 |  | 父ASIN（与 parent_seller_sku 成套使用） |
| `parent_seller_sku` | 字符串 |  | 父Seller SKU（与 parent_asin 成套使用） |

**出参**：售价 / 星级 / 评论数 / 变体数量 / 标题 / 大类排名 / 小类排名 / 链接转化率 / 类目转化率 / 类目退换货率 / 16周退款率 / 32周退款率 / 评论内容

---

### `listing_basic_info_v2`

Listing基础信息V2（精简版）：按店铺账号查询跟卖 Listing 的标题、星级、售价、16周退款率。相比 V1 去掉了 agent 不需要的字段，查询更快，推荐优先使用。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `parent_asin` | 字符串 |  | 父ASIN（与 parent_seller_sku 成套使用） |
| `parent_seller_sku` | 字符串 |  | 父Seller SKU（与 parent_asin 成套使用） |

**出参**：售价 / 星级 / 16周退款率 / 标题（精简版）

---

### `listing_inventory`

Listing库存快照：按父ASIN、父Seller SKU、店铺账号，返回各子ASIN最新库存快照（采购在途、待质检、FBA可售等）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |

**出参**：⚠️ 实测所有产品均返回空（子体库存快照数据源未落数据）

---

### `own_keyword_flow`

自身关键词流量：按父ASIN、父Seller SKU、店铺账号，返回跟卖子ASIN关联关键词的周搜索量、词的周排名、自然排位排名及自然位排位。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |

**出参**：关键词 / 周搜索量 / 词的周排名 / ASIN / 自然排位排名 / 自然位排位

---

### `parent_listing_detail`

查询父Listing详情：根据父ASIN获取父ASIN、父卖家SKU、产品中文名、产品名称、店铺账号、店铺id、站点等信息。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |

**出参**：店铺ID / 店铺账号 / 父ASIN / 父卖家SKU / 产品名称 / 产品中文名 / 站点

---

### `parent_listing_stock_summary`

查询父Listing库存汇总：按父ASIN、父Seller SKU、店铺账号，汇总所有跟卖子ASIN的FBA可售库存、FBA入库库存、FBA预留库存、FBA不可售库存（合计）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 |

**出参**：FBA可售库存 / FBA入库库存 / FBA预留库存 / FBA不可售库存

---

### `product_competitors`

产品竞品综合查询：基于竞品父ASIN与竞品ASIN，返回竞品ASIN详情、竞品评论列表、竞品预估月销量三块数据。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asins` | 字符串 |  | [asin详情] 竞品父ASIN列表，逗号分隔 |
| `asins` | 字符串 |  | [asin详情] 竞品ASIN列表，逗号分隔 |
| `sites` | 字符串 | ✅ | [跨模块] 站点列表（Amazon_US/Amazon_DE），逗号分隔，必填 |
| `top_category_rank_min` | 字符串 |  | [asin详情] 大类排名最小值 |
| `top_category_rank_max` | 字符串 |  | [asin详情] 大类排名最大值 |
| `listing_start_date` | 字符串 |  | [asin详情] 上架开始日期 yyyy-MM-dd |
| `listing_end_date` | 字符串 |  | [asin详情] 上架结束日期 yyyy-MM-dd |
| `crawl_start_date` | 字符串 |  | [asin详情] 抓取开始日期 yyyy-MM-dd |
| `crawl_end_date` | 字符串 |  | [asin详情] 抓取结束日期 yyyy-MM-dd |
| `last_category_ids` | 字符串 |  | [asin详情] 类目ID列表（last_category_id），逗号分隔 |
| `reviews_num_min` | 字符串 |  | [asin详情] 评论数最小值 |
| `reviews_num_max` | 字符串 |  | [asin详情] 评论数最大值 |
| `asin_detail_limit` | 字符串 |  | [asin详情] 详情列表最大返回条数，默认1000 |
| `review_asins` | 字符串 |  | [评论列表] 竞品ASIN列表（不传则继承竞品ASIN详情结果） |
| `review_sites` | 字符串 |  | [评论列表] 评论站点列表（不传则继承asin详情结果） |
| `review_star_min` | 字符串 |  | [评论列表] 评论星级最小值 |
| `review_star_max` | 字符串 |  | [评论列表] 评论星级最大值 |
| `review_start_date` | 字符串 |  | [评论列表] 评论开始日期 yyyy-MM-dd |
| `review_end_date` | 字符串 |  | [评论列表] 评论结束日期 yyyy-MM-dd |
| `review_limit` | 字符串 |  | [评论列表] 每个ASIN返回评论数量，默认20 |
| `sales_parent_asins` | 字符串 |  | [月销量] 竞品父ASIN列表（不传则继承竞品ASIN详情结果） |
| `sales_sites` | 字符串 |  | [月销量] 销量站点列表（不传则继承asin详情结果） |
| `month_start` | 字符串 |  | [月销量] 月销量开始月份 yyyy-MM 或 yyyy-MM-dd（不传默认近12个月） |
| `month_end` | 字符串 |  | [月销量] 月销量结束月份 yyyy-MM 或 yyyy-MM-dd（不传默认近12个月） |

**出参**：data + meta（竞品综合，含竞品详情/评论/月销量三块）

---

### `product_competitors_asin_detail`

产品竞品ASIN详情查询：按竞品父ASIN与竞品ASIN筛选，返回竞品ASIN详情（站点、标题、类目排名、抓取时间等）。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asins` | 字符串 |  | [asin详情] 竞品父ASIN列表，逗号分隔 |
| `asins` | 字符串 |  | [asin详情] 竞品ASIN列表，逗号分隔 |
| `sites` | 字符串 | ✅ | [asin详情] 站点列表（Amazon_US/Amazon_DE），逗号分隔，必填 |
| `top_category_rank_min` | 字符串 |  | [asin详情] 大类排名最小值 |
| `top_category_rank_max` | 字符串 |  | [asin详情] 大类排名最大值 |
| `listing_start_date` | 字符串 |  | [asin详情] 上架开始日期 yyyy-MM-dd |
| `listing_end_date` | 字符串 |  | [asin详情] 上架结束日期 yyyy-MM-dd |
| `crawl_start_date` | 字符串 |  | [asin详情] 抓取开始日期 yyyy-MM-dd |
| `crawl_end_date` | 字符串 |  | [asin详情] 抓取结束日期 yyyy-MM-dd |
| `last_category_ids` | 字符串 |  | [asin详情] 类目ID列表（last_category_id），逗号分隔 |
| `reviews_num_min` | 字符串 |  | [asin详情] 评论数最小值 |
| `reviews_num_max` | 字符串 |  | [asin详情] 评论数最大值 |
| `asin_detail_limit` | 字符串 |  | [asin详情] 详情列表最大返回条数，默认1000 |

**出参**：（需竞品爬虫库有该ASIN，多为空）

---

### `product_competitors_basic_info`

竞品基础信息查询：按 ASIN 与站点返回竞品基础信息（含月销量、月销售额、日均销量）及评论列表两块数据。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asin` | 字符串 | ✅ | 竞品 ASIN |
| `site_code` | 字符串 | ✅ | 站点代码: Amazon_US / Amazon_UK / Amazon_DE，也支持 US/DE 简写 |
| `review_limit` | 字符串 |  | [评论列表] 返回评论数量，默认20 |

**出参**：data（竞品基础信息含月销量/日均销量 + 评论列表）

---

### `product_competitors_monthly_sales`

产品竞品预估月销量查询：基于竞品父ASIN与站点查询，按月份返回竞品月销量；不传月份默认近12个月。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sales_parent_asins` | 字符串 | ✅ | [月销量] 竞品父ASIN列表，逗号分隔，必填 |
| `sales_sites` | 字符串 | ✅ | [月销量] 销量站点列表（Amazon_US/Amazon_DE），逗号分隔，必填 |
| `month_start` | 字符串 |  | [月销量] 月销量开始月份 yyyy-MM 或 yyyy-MM-dd（不传默认近12个月） |
| `month_end` | 字符串 |  | [月销量] 月销量结束月份 yyyy-MM 或 yyyy-MM-dd（不传默认近12个月） |

**出参**：（同上）

---

### `product_competitors_review_list`

产品竞品评论列表查询：基于竞品ASIN与站点筛选，支持星级/评论时间过滤，并按每个竞品ASIN限制返回条数。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `review_asins` | 字符串 | ✅ | [评论列表] 竞品ASIN列表，逗号分隔，必填 |
| `review_sites` | 字符串 | ✅ | [评论列表] 评论站点列表（Amazon_US/Amazon_DE），逗号分隔，必填 |
| `review_star_min` | 字符串 |  | [评论列表] 评论星级最小值 |
| `review_star_max` | 字符串 |  | [评论列表] 评论星级最大值 |
| `review_start_date` | 字符串 |  | [评论列表] 评论开始日期 yyyy-MM-dd |
| `review_end_date` | 字符串 |  | [评论列表] 评论结束日期 yyyy-MM-dd |
| `review_limit` | 字符串 |  | [评论列表] 每个ASIN返回评论数量，默认20 |

**出参**：🔒 当前 apiKey 无权限

---

### `product_sales`

查询产品销售额、销量、单量、广告花费、ACOS等关键经营指标。需要提供父ASIN、父SKU、店铺账号和时间范围。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN (如 B0XXXXX) |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 shop_us) |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：全部单量 / 全部销量 / 全部销售额 / 广告花费 / 广告销售额 / 广告单量 / ACOS / 毛利率（父体级，区间聚合1行）

---

### `sales_performance`

销售业绩模板：按父ASIN、父Seller SKU、店铺账号与时间范围，按子ASIN返回销售额(人民币)、销量、单量、客单价、CTR、CVR、售价、大类排名、退款率(16周)、广告单量占比、毛利、毛利率、仓租占比、当月及后3月目标销量、本月完成、不满意率。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_asin` | 字符串 | ✅ | 父ASIN |
| `parent_seller_sku` | 字符串 | ✅ | 父Seller SKU |
| `shop_account` | 字符串 | ✅ | 店铺账号 (如 am_example_us) |
| `start_date` | 字符串 | ✅ | 开始日期 yyyy-MM-dd |
| `end_date` | 字符串 | ✅ | 结束日期 yyyy-MM-dd |

**出参**：按子体：asin / seller_sku / 单量 / 销量 / 销售额 / 售价 / 客单价 / CTR / CVR / 毛利 / 毛利率 / 仓租占比 / 广告单量占比 / 大类排名 / 退款率(16周) / 不满意率 / 当月目标销量 / 后1月目标销量 / 后2月目标销量 / 后3月目标销量 / 本月完成

---

### `temu_goods_sku_query`

分页查询TEMU商品SKU数据。默认查询FBA库存、SKU扩展、销量、海外仓库存。支持按外部编码模糊、仓库SKU模糊、卖家SKU模糊/精确、店铺等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `extCodeLike` | 字符串 |  | 外部编码模糊 |
| `extCode` | 字符串 |  | 外部编码精确 |
| `originSkuLike` | 字符串 |  | 仓库SKU模糊 |
| `originSku` | 字符串 |  | 仓库SKU精确 |
| `sellerSkuLike` | 字符串 |  | 卖家SKU模糊 |
| `sellerSku` | 字符串 |  | 卖家SKU精确 |
| `shopId` | 字符串 |  | 店铺ID |
| `pageNo` | 整数 |  | 页码，默认1 |
| `pageSize` | 整数 |  | 每页条数，默认100 |

**出参**：🔒 无权限（TEMU）

---

### `temu_goods_stock_query`

分页查询TEMU平台产品库存数据。支持按店铺、SKC、SKU等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopId` | 字符串 |  | 店铺ID |
| `skcId` | 整数 |  | SKC ID |
| `skuId` | 整数 |  | SKU ID |
| `warehouseId` | 整数 |  | 仓库ID |
| `warehouseName` | 字符串 |  | 仓库名称 |
| `pageNo` | 整数 |  | 页码，默认1 |
| `pageSize` | 整数 |  | 每页条数，默认100 |

**出参**：🔒 无权限（TEMU）

---

### `temu_map_relation_query`

分页查询TEMU映射关系数据。支持按仓库SKU模糊、TEMU卖家SKU模糊、亚马逊FNSKU模糊、店铺、映射类型等条件筛选。支持分页。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `originSkuLike` | 字符串 |  | 仓库SKU模糊 |
| `temuSellerSkuLike` | 字符串 |  | TEMU卖家SKU模糊 |
| `temuSellerSku` | 字符串 |  | TEMU卖家SKU精确 |
| `azFnSkuLike` | 字符串 |  | 亚马逊FNSKU模糊 |
| `azFnSku` | 字符串 |  | 亚马逊FNSKU精确 |
| `shopId` | 字符串 |  | 店铺ID |
| `azShopId` | 字符串 |  | 亚马逊店铺ID |
| `mapRelationType` | 字符串 |  | 映射类型 |
| `pageNo` | 整数 |  | 页码，默认1 |
| `pageSize` | 整数 |  | 每页条数，默认100 |

**出参**：🔒 无权限（TEMU）

---

### `whp_amazon_advert_keyword_suggest_bid`

查询广告关键词建议出价列表。根据店铺账号(parentAsin/parentSellerSku)和关键词列表，获取广告关键词的建议出价信息。支持通过店铺账号(shopAccount)自动查询店铺ID。

**入参**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `shopAccount` | 字符串 | ✅ | 店铺账号（如 am_example_us），内部自动通过dwd_shop表查询对应的店铺ID |
| `parentAsin` | 字符串 | ✅ | 父ASIN（亚马逊标准识别码），如 B0DQLB8WWC |
| `parentSellerSku` | 字符串 | ✅ | 父卖家SKU |
| `keywordVoList` | 字符串 | ✅ | 关键词列表，JSON 数组格式，如 [{"keyword":"wireless mouse"}] |

**出参**：关键词建议出价（keywordVoList 需 JSON 数组格式 [{"keyword":"..."}]）

---
