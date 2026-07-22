-- 业务巡检 Agent · 本地数据缓存库 (SQLite)
-- 设计原则：
--   1. 每张表都有 data(JSON) 列，原封不动存 MCP 返回的全部原生字段 → 加新字段零迁移
--   2. 热点查询字段用「生成列」(VIRTUAL) 从 data 抽取，列名 = MCP 原生中文字段名
--   3. 加新字段：① 直接 json_extract(data,'$.字段') 读；或 ② 加一行生成列 DDL（见文末 promote 示例）
--   4. 维度列 (asin/parent_asin/shop_account/site_code/stat_date) 建索引，供巡检快速筛选
--   5. 每日类表带 is_frozen：归因窗口外(>14天)冻结，永不重取

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- 1. 每日销售指标 (product_sales，单日窗口循环取，可永久累积)
-- 原生字段：全部单量/广告花费/毛利率/ACOS/全部销量/全部销售额/广告单量/广告销售额
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_product_sales (
  asin              TEXT NOT NULL,
  parent_asin       TEXT,
  shop_account      TEXT NOT NULL,
  site_code         TEXT,
  stat_date         TEXT NOT NULL,          -- YYYY-MM-DD
  data              TEXT NOT NULL,          -- 原生字段 JSON
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  is_frozen         INTEGER NOT NULL DEFAULT 0,
  "全部单量"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."全部单量"'))   VIRTUAL,
  "全部销量"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."全部销量"'))   VIRTUAL,
  "全部销售额"        REAL    GENERATED ALWAYS AS (json_extract(data,'$."全部销售额"')) VIRTUAL,
  "广告花费"          REAL    GENERATED ALWAYS AS (json_extract(data,'$."广告花费"'))   VIRTUAL,
  "广告销售额"        REAL    GENERATED ALWAYS AS (json_extract(data,'$."广告销售额"')) VIRTUAL,
  "广告单量"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."广告单量"'))   VIRTUAL,
  "ACOS"             REAL    GENERATED ALWAYS AS (json_extract(data,'$."ACOS"'))      VIRTUAL,
  "毛利率"            REAL    GENERATED ALWAYS AS (json_extract(data,'$."毛利率"'))     VIRTUAL,
  PRIMARY KEY (asin, shop_account, stat_date)
);
CREATE INDEX IF NOT EXISTS idx_dps_parent ON daily_product_sales(parent_asin, stat_date);
CREATE INDEX IF NOT EXISTS idx_dps_shop   ON daily_product_sales(shop_account);

-- ============================================================
-- 2. 每日广告产品报告 (ad_product_report，单日窗口循环取)
-- 原生字段：CTR/销售额/币种/广告订单量/CPC/花费/ACOS/销售数量/点击量/CVR/曝光量
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_ad_product (
  asin              TEXT NOT NULL,
  parent_asin       TEXT,
  shop_account      TEXT NOT NULL,
  site_code         TEXT,
  stat_date         TEXT NOT NULL,
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  is_frozen         INTEGER NOT NULL DEFAULT 0,
  "花费"             REAL    GENERATED ALWAYS AS (json_extract(data,'$."花费"'))      VIRTUAL,
  "销售额"           REAL    GENERATED ALWAYS AS (json_extract(data,'$."销售额"'))    VIRTUAL,
  "ACOS"            REAL    GENERATED ALWAYS AS (json_extract(data,'$."ACOS"'))     VIRTUAL,
  "点击量"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$."点击量"'))    VIRTUAL,
  "曝光量"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$."曝光量"'))    VIRTUAL,
  "广告订单量"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$."广告订单量"')) VIRTUAL,
  "CPC"             REAL    GENERATED ALWAYS AS (json_extract(data,'$."CPC"'))      VIRTUAL,
  "CTR"             REAL    GENERATED ALWAYS AS (json_extract(data,'$."CTR"'))      VIRTUAL,
  "CVR"             REAL    GENERATED ALWAYS AS (json_extract(data,'$."CVR"'))      VIRTUAL,
  PRIMARY KEY (asin, shop_account, stat_date)
);
CREATE INDEX IF NOT EXISTS idx_dap_parent ON daily_ad_product(parent_asin, stat_date);

-- ============================================================
-- 3. 子体销售 + 月度目标 (sales_performance，快照，每月/每日刷)
-- 原生字段：CTR/售价/退款率(16周)/广告单量占比/毛利/后1|2|3月目标销量/当月目标销量/本月完成/客单价/毛利率/仓租占比/大类排名/不满意率/单量/销售额/销量/CVR/asin/seller_sku
-- ============================================================
CREATE TABLE IF NOT EXISTS sales_child (
  asin              TEXT NOT NULL,
  parent_asin       TEXT NOT NULL,
  shop_account      TEXT NOT NULL,
  seller_sku        TEXT,
  stat_month        TEXT,                   -- YYYY-MM 快照月
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "当月目标销量"      REAL GENERATED ALWAYS AS (json_extract(data,'$."当月目标销量"')) VIRTUAL,
  "后1月目标销量"     REAL GENERATED ALWAYS AS (json_extract(data,'$."后1月目标销量"')) VIRTUAL,
  "后2月目标销量"     REAL GENERATED ALWAYS AS (json_extract(data,'$."后2月目标销量"')) VIRTUAL,
  "后3月目标销量"     REAL GENERATED ALWAYS AS (json_extract(data,'$."后3月目标销量"')) VIRTUAL,
  "本月完成"          REAL GENERATED ALWAYS AS (json_extract(data,'$."本月完成"'))     VIRTUAL,
  "单量"             REAL GENERATED ALWAYS AS (json_extract(data,'$."单量"'))        VIRTUAL,
  "销量"             REAL GENERATED ALWAYS AS (json_extract(data,'$."销量"'))        VIRTUAL,
  "毛利率"            REAL GENERATED ALWAYS AS (json_extract(data,'$."毛利率"'))       VIRTUAL,
  "大类排名"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."大类排名"'))   VIRTUAL,
  PRIMARY KEY (asin, parent_asin, shop_account)
);
CREATE INDEX IF NOT EXISTS idx_sc_parent ON sales_child(parent_asin);

-- ============================================================
-- 4. Listing 基线 (listing_basic_info，快照，每日/每周刷)
-- 原生字段：售价/星级/评论数/变体数量/类目退换货率/大类排名/32周退款率/链接转化率/评论内容/类目转化率/16周退款率/标题/小类排名
-- ============================================================
CREATE TABLE IF NOT EXISTS listing_baseline (
  parent_asin       TEXT NOT NULL,
  shop_account      TEXT NOT NULL,
  site_code         TEXT,
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "售价"             REAL GENERATED ALWAYS AS (json_extract(data,'$."售价"'))        VIRTUAL,
  "星级"             REAL GENERATED ALWAYS AS (json_extract(data,'$."星级"'))        VIRTUAL,
  "评论数"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$."评论数"'))    VIRTUAL,
  "链接转化率"        REAL GENERATED ALWAYS AS (json_extract(data,'$."链接转化率"'))    VIRTUAL,
  "类目转化率"        REAL GENERATED ALWAYS AS (json_extract(data,'$."类目转化率"'))    VIRTUAL,
  "类目退换货率"      REAL GENERATED ALWAYS AS (json_extract(data,'$."类目退换货率"'))  VIRTUAL,
  "16周退款率"        REAL GENERATED ALWAYS AS (json_extract(data,'$."16周退款率"'))   VIRTUAL,
  "32周退款率"        REAL GENERATED ALWAYS AS (json_extract(data,'$."32周退款率"'))   VIRTUAL,
  "大类排名"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."大类排名"'))   VIRTUAL,
  "小类排名"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."小类排名"'))   VIRTUAL,
  PRIMARY KEY (parent_asin, shop_account)
);

-- ============================================================
-- 5. FBA 库存汇总 (parent_listing_stock_summary，短TTL，当天快照)
-- 原生字段：FBA不可售库存/FBA可售库存/FBA入库库存/FBA预留库存
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_summary (
  parent_asin       TEXT NOT NULL,
  shop_account      TEXT NOT NULL,
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "FBA可售库存"      INTEGER GENERATED ALWAYS AS (json_extract(data,'$."FBA可售库存"'))   VIRTUAL,
  "FBA入库库存"      INTEGER GENERATED ALWAYS AS (json_extract(data,'$."FBA入库库存"'))   VIRTUAL,
  "FBA预留库存"      INTEGER GENERATED ALWAYS AS (json_extract(data,'$."FBA预留库存"'))   VIRTUAL,
  "FBA不可售库存"    INTEGER GENERATED ALWAYS AS (json_extract(data,'$."FBA不可售库存"')) VIRTUAL,
  PRIMARY KEY (parent_asin, shop_account)
);

-- ============================================================
-- 6. 产品扩展标签 (az_extend_detail 120字段，快照，每日刷)
-- 全部字段进 data；提升打分/判定要用的关键标签为生成列
-- ============================================================
CREATE TABLE IF NOT EXISTS product_tags (
  asin              TEXT NOT NULL,
  shop_account      TEXT NOT NULL,
  seller_sku        TEXT,
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "SEASONALITY"          TEXT GENERATED ALWAYS AS (json_extract(data,'$.SEASONALITY'))          VIRTUAL, -- 淡旺季
  "PRODUCT_GRADE"        TEXT GENERATED ALWAYS AS (json_extract(data,'$.PRODUCT_GRADE'))        VIRTUAL, -- 产品等级
  "TARGET_STAR_RATE"     REAL GENERATED ALWAYS AS (json_extract(data,'$.TARGET_STAR_RATE'))     VIRTUAL, -- 目标评分
  "STAR_LEVEL"           REAL GENERATED ALWAYS AS (json_extract(data,'$.STAR_LEVEL'))           VIRTUAL,
  "STOCK_INVENTORY"      INTEGER GENERATED ALWAYS AS (json_extract(data,'$.STOCK_INVENTORY'))   VIRTUAL,
  "ASIN_START_SALE_DATE" TEXT GENERATED ALWAYS AS (json_extract(data,'$.ASIN_START_SALE_DATE')) VIRTUAL, -- 上架日
  "REFUND_RATE"          REAL GENERATED ALWAYS AS (json_extract(data,'$.REFUND_RATE'))          VIRTUAL,
  "COMMENT_NUM"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$.COMMENT_NUM'))        VIRTUAL,
  PRIMARY KEY (asin, shop_account)
);

-- ============================================================
-- 7. 直接竞品 (direct_competitors，快照，每周刷)
-- ============================================================
CREATE TABLE IF NOT EXISTS competitors (
  parent_asin       TEXT NOT NULL,
  shop_account      TEXT NOT NULL,
  competitor_asin   TEXT NOT NULL,
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "品牌"             TEXT GENERATED ALWAYS AS (json_extract(data,'$."品牌"'))          VIRTUAL,
  "星级"             REAL GENERATED ALWAYS AS (json_extract(data,'$."星级"'))          VIRTUAL,
  "评论数"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$."评论数"'))      VIRTUAL,
  "末级类目排名"      INTEGER GENERATED ALWAYS AS (json_extract(data,'$."末级类目排名"')) VIRTUAL,
  PRIMARY KEY (parent_asin, competitor_asin)
);

-- ============================================================
-- 8. 关键词流量 (own_keyword_flow / flow_keywords，快照，每周刷)
-- ============================================================
CREATE TABLE IF NOT EXISTS keyword_flow (
  parent_asin       TEXT,
  asin              TEXT,
  shop_account      TEXT NOT NULL,
  keyword           TEXT NOT NULL,
  source            TEXT NOT NULL,          -- own_keyword_flow | flow_keywords
  data              TEXT NOT NULL,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "周搜索量"          INTEGER GENERATED ALWAYS AS (json_extract(data,'$."周搜索量"'))    VIRTUAL,
  "自然排位排名"      INTEGER GENERATED ALWAYS AS (json_extract(data,'$."自然排位排名"')) VIRTUAL,
  PRIMARY KEY (shop_account, keyword, source, asin)
);

-- ============================================================
-- 9. 同步日志 (增量同步用：记录每个 asin/date/tool 是否已取)
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  tool              TEXT NOT NULL,
  target_key        TEXT NOT NULL,          -- asin 或 parent_asin
  stat_date         TEXT,                   -- 每日类才有
  status            TEXT NOT NULL,          -- ok | empty | error
  rows_count        INTEGER DEFAULT 0,
  note              TEXT,
  fetched_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_synclog ON sync_log(tool, target_key, stat_date);

-- ============================================================
-- 10. 事件池 (event_pool)
-- 来源：docs/业务巡检Agent-代码实现版.md §3.5、§5，巡检频次方案 §9
-- 一条事件 = 一个"父ASIN × 问题点位 × 命中变体" 的生命周期实例
-- 10 态状态机（新发现/待确认/处理中/待观察/长期跟进/已处理待复扫/已关闭/误报/忽略/人工中断）
-- ============================================================
CREATE TABLE IF NOT EXISTS event_pool (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  唯一识别              TEXT NOT NULL UNIQUE,        -- 店铺+父ASIN+问题点位+命中变体的 hash
  -- 定位（对齐模块 7.1 A 段）
  店铺账号              TEXT NOT NULL,
  站点                  TEXT,
  父ASIN                TEXT NOT NULL,
  父SKU                 TEXT,
  -- 异常内容（对齐模块 7.1 C 段）
  异常大类              TEXT,                        -- 如 '2.4 库存与可售'
  问题点位              TEXT NOT NULL,               -- 如 'FBA可售库存为0'
  作用层级              TEXT NOT NULL,               -- '链接级' | '变体级'
  命中变体              TEXT,                        -- 子ASIN，如 'B0XXX' 或 '黑色/M'
  变体重要性            TEXT,                        -- '主要色' | '次要色' | '长尾色' | null
  异常类型              TEXT,                        -- '现象即原因型' | '表现型'
  -- 判定结果
  严重度                TEXT,                        -- 'S0' | 'S1' | 'S2'
  是否共因上调          INTEGER DEFAULT 0,           -- 0/1
  初判严重度            TEXT,                        -- 未上调前的原始 S
  单异常执行分数        REAL,
  -- 状态机（§5）
  当前状态              TEXT NOT NULL DEFAULT '新发现',
  -- 生命周期时间
  首次命中时间          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  最近命中时间          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  下一次复查时间        TEXT,                        -- 由 R_巡检频次 或运营设置
  复查时间来源          TEXT,                        -- 'default' | 'manual'
  关闭时间              TEXT,
  关闭原因              TEXT,                        -- '自动恢复' | '人工已处理' | '误报' | '忽略'
  -- 处理动作历史
  上次处理动作          TEXT,
  上次处理时间          TEXT,
  上次处理人            TEXT,
  -- 复发跟踪
  复发次数              INTEGER DEFAULT 0,
  -- 判定依据全量存 JSON（可扩展，不动 schema）
  判定依据              TEXT NOT NULL,               -- {判定过程, 触发字段, 判定日志}
  处理备注              TEXT,                        -- JSON: {误报原因/忽略期限/长期跟进类型 等}
  -- 关联
  最近巡检批次          TEXT,                        -- 巡检批次号
  inspection_result_id INTEGER,
  参数版本              TEXT,                        -- 用哪一版 R2/R3/R4 判的
  -- 追踪
  创建时间              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  更新时间              TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_event_asin ON event_pool(父ASIN, 当前状态);
CREATE INDEX IF NOT EXISTS idx_event_status ON event_pool(当前状态);
CREATE INDEX IF NOT EXISTS idx_event_recheck ON event_pool(下一次复查时间, 当前状态);
CREATE INDEX IF NOT EXISTS idx_event_shop ON event_pool(店铺账号);

-- ============================================================
-- 11. 事件状态流转日志 (event_state_log)
-- 每次状态变更留痕，用于回溯"这个事件为什么升级/关闭"
-- ============================================================
CREATE TABLE IF NOT EXISTS event_state_log (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id              INTEGER NOT NULL REFERENCES event_pool(id) ON DELETE CASCADE,
  变更时间              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  变更前状态            TEXT,
  变更后状态            TEXT,
  变更前严重度          TEXT,
  变更后严重度          TEXT,
  变更类型              TEXT NOT NULL,               -- 'auto升级' | 'auto降级' | '人工降级' | '共因上调' | '共因回落' | '状态流转' | '解除关闭'
  变更原因              TEXT NOT NULL,               -- 一句人话
  操作人                TEXT,                        -- 系统|运营账号
  上下文                TEXT                          -- JSON 附加信息
);
CREATE INDEX IF NOT EXISTS idx_event_log_eid ON event_state_log(event_id, 变更时间);

-- ============================================================
-- 12. 巡检批次 (inspection_batch)
-- 一次巡检运行的元数据，方便按批次查历史
-- ============================================================
CREATE TABLE IF NOT EXISTS inspection_batch (
  批次号                TEXT PRIMARY KEY,            -- 如 '20260709-1030'
  开始时间              TEXT NOT NULL,
  结束时间              TEXT,
  触发类型              TEXT NOT NULL,               -- 'daily' | '变更触发' | '表现触发' | '广告触发' | '高风险触发' | '人工触发' | '心跳'
  触发人                TEXT,
  巡检范围              TEXT NOT NULL,               -- JSON: {父ASIN列表, 模块列表}
  参数版本              TEXT,                        -- R2/R3/R4/R5 版本
  统计                  TEXT,                        -- JSON: {扫描父ASIN数, 命中异常数, 观察数, 数据不足数}
  状态                  TEXT NOT NULL DEFAULT '进行中'  -- '进行中' | '完成' | '失败'
);

-- ============================================================
-- 12.1 巡检结果快照 (inspection_result)
-- 每个批次、父ASIN、店铺一条；保留完整任务卡 JSON，供前端/历史查询
-- ============================================================
CREATE TABLE IF NOT EXISTS inspection_result (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no           TEXT NOT NULL,
  parent_asin        TEXT NOT NULL,
  shop_account       TEXT NOT NULL,
  site_code          TEXT,
  result_status      TEXT NOT NULL,                -- SUCCESS | FAILED
  priority           TEXT,
  score              REAL,
  anomaly_count      INTEGER NOT NULL DEFAULT 0,
  result_json        TEXT NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(batch_no, parent_asin, shop_account)
);
CREATE INDEX IF NOT EXISTS idx_inspection_result_product
  ON inspection_result(parent_asin, shop_account, created_at);
CREATE INDEX IF NOT EXISTS idx_inspection_result_batch
  ON inspection_result(batch_no, result_status);

-- ============================================================
-- 13. 每日自然/广告订单流 (daily_natural_ad_flow)
-- 来源：erp_listing_natural_advert_flow (azlisting-mcpserver)
-- 一次请求 = 父ASIN × 日期 × 子体 三维数据
-- 支撑：§3.8 自然流量异常 · §3.12 放量未执行 条件5
--
-- ⚠️ 币种说明：
--   金额字段（adCostAmountCny/adSaleAmountCny/totalSaleAmountCny）单位为 CNY
--   由 MCP 内部按固定汇率 6.6 换算（实测精确 = 6.600 与旧工具 product_sales 的 USD 值对应）
--   本项目判定金额一律走 daily_product_sales（USD）；本表金额字段仅供交叉验证，不参与判定
--   本表**只消费整数字段**：naturalOrderNum / totalOrderNum / adOrderNum / adClick / adImpressions / adSaleNum
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_natural_ad_flow (
  asin                TEXT NOT NULL,               -- 子ASIN
  parent_asin         TEXT NOT NULL,
  shop_account        TEXT NOT NULL,
  seller_sku          TEXT,
  parent_seller_sku   TEXT,
  stat_date           TEXT NOT NULL,               -- YYYY-MM-DD
  is_summary          INTEGER DEFAULT 0,           -- 是否父级汇总行
  data                TEXT NOT NULL,               -- 原生字段 JSON
  fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  is_frozen           INTEGER NOT NULL DEFAULT 0,
  "adOrderNum"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.adOrderNum'))        VIRTUAL,
  "totalOrderNum"     INTEGER GENERATED ALWAYS AS (json_extract(data,'$.totalOrderNum'))     VIRTUAL,
  "naturalOrderNum"   INTEGER GENERATED ALWAYS AS (json_extract(data,'$.naturalOrderNum'))   VIRTUAL,
  "adCostAmountCny"   REAL    GENERATED ALWAYS AS (json_extract(data,'$.adCostAmountCny'))   VIRTUAL,
  "adSaleAmountCny"   REAL    GENERATED ALWAYS AS (json_extract(data,'$.adSaleAmountCny'))   VIRTUAL,
  "totalSaleAmountCny" REAL   GENERATED ALWAYS AS (json_extract(data,'$.totalSaleAmountCny')) VIRTUAL,
  "tacos"             REAL    GENERATED ALWAYS AS (json_extract(data,'$.tacos'))             VIRTUAL,
  "adClick"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$.adClick'))           VIRTUAL,
  "adImpressions"     INTEGER GENERATED ALWAYS AS (json_extract(data,'$.adImpressions'))     VIRTUAL,
  "adSaleNum"         INTEGER GENERATED ALWAYS AS (json_extract(data,'$.adSaleNum'))         VIRTUAL,
  PRIMARY KEY (asin, shop_account, stat_date, is_summary)
);
CREATE INDEX IF NOT EXISTS idx_naf_parent ON daily_natural_ad_flow(parent_asin, stat_date);
CREATE INDEX IF NOT EXISTS idx_naf_shop   ON daily_natural_ad_flow(shop_account);

-- ============================================================
-- 14. 月度目标 (monthly_goal)
-- 来源：erp_listing_monthly_goal (azlisting-mcpserver)
-- 当月 + 未来3月，每父ASIN 4 行
-- 支撑：§3.7 目标偏离 · §3.13 库存积压 B档
-- ============================================================
CREATE TABLE IF NOT EXISTS monthly_goal (
  parent_asin         TEXT NOT NULL,
  parent_seller_sku   TEXT,
  shop_account        TEXT NOT NULL,
  month_str           TEXT NOT NULL,               -- 如 '2026年07月'
  data                TEXT NOT NULL,
  fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "estimatedMonthOrderNum" INTEGER GENERATED ALWAYS AS (json_extract(data,'$.estimatedMonthOrderNum')) VIRTUAL,
  "targetRank"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.targetRank'))       VIRTUAL,
  "targetRatio"       REAL    GENERATED ALWAYS AS (json_extract(data,'$.targetRatio'))      VIRTUAL,
  "asinPrincipalUserName" TEXT GENERATED ALWAYS AS (json_extract(data,'$.asinPrincipalUserName')) VIRTUAL,
  PRIMARY KEY (parent_asin, shop_account, month_str)
);
CREATE INDEX IF NOT EXISTS idx_goal_parent ON monthly_goal(parent_asin);

-- ============================================================
-- 15. 库存预警全维度 (stock_alert)
-- 来源：erp_listing_stock_alert (azlisting-mcpserver)
-- 相对旧 stock_summary 更全（15+ 库存维度 + 预计缺货日/天数）
-- 支撑：§2.4 库存不足 · §3.13 库存积压 · §3.14 滞销
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_alert (
  parent_asin         TEXT NOT NULL,
  parent_seller_sku   TEXT,
  shop_account        TEXT NOT NULL,
  data                TEXT NOT NULL,
  fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "canSaleNum"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.canSaleNum'))         VIRTUAL,
  "inStockNum"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.inStockNum'))         VIRTUAL,
  "inStockWorkingNum" INTEGER GENERATED ALWAYS AS (json_extract(data,'$.inStockWorkingNum'))  VIRTUAL,
  "inStockReceivingNum" INTEGER GENERATED ALWAYS AS (json_extract(data,'$.inStockReceivingNum')) VIRTUAL,
  "reserveNum"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.reserveNum'))         VIRTUAL,
  "noSaleNum"         INTEGER GENERATED ALWAYS AS (json_extract(data,'$.noSaleNum'))          VIRTUAL,
  "investigationNum"  INTEGER GENERATED ALWAYS AS (json_extract(data,'$.investigationNum'))   VIRTUAL,
  "transferInStock"   INTEGER GENERATED ALWAYS AS (json_extract(data,'$.transferInStock'))    VIRTUAL,
  "directShipStock"   INTEGER GENERATED ALWAYS AS (json_extract(data,'$.directShipStock'))    VIRTUAL,
  "totalStock"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.totalStock'))         VIRTUAL,
  "purchaseOnWay"     INTEGER GENERATED ALWAYS AS (json_extract(data,'$.purchaseOnWay'))      VIRTUAL,
  PRIMARY KEY (parent_asin, shop_account)
);
CREATE INDEX IF NOT EXISTS idx_stockalert_parent ON stock_alert(parent_asin);

-- ============================================================
-- 16. 超龄仓租费用 (inventory_cost)
-- 来源：erp_listing_inventory_cost_analysis (azlisting-mcpserver)
-- 一个父ASIN N 个子ASIN，每个子ASIN 有 longTermStorageFees 数组
-- 支撑：§3.14 滞销异常判定必需
-- ============================================================
CREATE TABLE IF NOT EXISTS inventory_cost (
  parent_asin         TEXT NOT NULL,
  parent_seller_sku   TEXT,
  shop_account        TEXT NOT NULL,
  child_asin          TEXT NOT NULL,               -- 子ASIN
  seller_sku          TEXT,
  fn_sku              TEXT,
  report_month        TEXT NOT NULL,               -- 如 '2026-07'
  data                TEXT NOT NULL,               -- 原生 {longTermStorageFees:[{qtyCharged,amountCharged,surchargeAgeTier}]}
  -- 汇总列（写入时预算好，避免生成列子查询限制）
  "汇总超龄库存数"    INTEGER,                     -- SUM(qtyCharged)
  "汇总超龄仓租费"    REAL,                        -- SUM(amountCharged)  单位 USD
  fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (parent_asin, child_asin, report_month)
);
CREATE INDEX IF NOT EXISTS idx_invcost_parent ON inventory_cost(parent_asin, report_month);

-- ============================================================
-- 17. 子体实时价格促销快照 (child_price_promo)
-- 来源：erp_listing_price_promotion_analysis (azlisting-mcpserver，实时爬子ASIN)
-- 每子ASIN × 抓取日期一条快照；建议每周一次（实时爬慢）
-- 支撑：
--   §2.3 变体价差异常（price + coupon 到手价对比）
--   §2.3 促销异常（前台促销展示 vs ERP配置）
--   §2.6 类目异常（categoryName + breadCrumbs 类目路径）
--   §3.11 评分基准B（star vs 目标评分，替代常空的 CRAW_ASIN_STAR）
--
-- 字段说明：
--   price 来自 MCP 是"$12.99"字符串格式；price_usd 由代码写入时解析为数字
--   bestSellersRank 是原始文本（可能多类目排名），bestSellersRankItems 是结构化数组
-- ============================================================
CREATE TABLE IF NOT EXISTS child_price_promo (
  child_asin          TEXT NOT NULL,
  parent_asin         TEXT NOT NULL,
  shop_account        TEXT NOT NULL,
  site_code           TEXT NOT NULL,
  snapshot_date       TEXT NOT NULL,               -- YYYY-MM-DD
  data                TEXT NOT NULL,               -- 原生完整 JSON
  price_usd           REAL,                        -- 写入时从 "$12.99" 剥出，判定用
  fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "price"             TEXT GENERATED ALWAYS AS (json_extract(data,'$.price'))             VIRTUAL,
  "coupon"            TEXT GENERATED ALWAYS AS (json_extract(data,'$.coupon'))            VIRTUAL,
  "strikethroughPrice" TEXT GENERATED ALWAYS AS (json_extract(data,'$.strikethroughPrice')) VIRTUAL,
  "savingsPercentage" TEXT GENERATED ALWAYS AS (json_extract(data,'$.savingsPercentage')) VIRTUAL,
  "star"              REAL GENERATED ALWAYS AS (json_extract(data,'$.star'))              VIRTUAL,
  "ratingsNum"        INTEGER GENERATED ALWAYS AS (json_extract(data,'$.ratingsNum'))     VIRTUAL,
  "bestSellersRank"   TEXT GENERATED ALWAYS AS (json_extract(data,'$.bestSellersRank'))   VIRTUAL,
  "categoryName"      TEXT GENERATED ALWAYS AS (json_extract(data,'$.categoryName'))      VIRTUAL,
  "inStock"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$.inStock'))        VIRTUAL,
  "hasCart"           INTEGER GENERATED ALWAYS AS (json_extract(data,'$.hasCart'))        VIRTUAL,
  PRIMARY KEY (child_asin, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_ppromo_parent ON child_price_promo(parent_asin, snapshot_date);

-- ============================================================
-- 17.2 巡检扩展快照（新版 azlisting MCP）
-- 每天按来源保留一份原始返回，供巡检和复盘使用。
-- ============================================================
CREATE TABLE IF NOT EXISTS listing_inspection_snapshot (
  parent_asin        TEXT NOT NULL,
  parent_seller_sku  TEXT,
  shop_account       TEXT NOT NULL,
  snapshot_date      TEXT NOT NULL,
  source             TEXT NOT NULL,
  data               TEXT NOT NULL,
  fetched_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (parent_asin, shop_account, snapshot_date, source)
);
CREATE INDEX IF NOT EXISTS idx_inspection_snapshot_latest
  ON listing_inspection_snapshot(parent_asin, shop_account, source, snapshot_date DESC);

-- ============================================================
-- 17.1 Listing 前台详情快照（pangolinfo_api_sync_Extract，按需实时抓取）
-- 用途：链接可购性、主图/副图、A+ 内容、前台价格优惠的真实页面证据。
-- 注意：该来源不提供后台抑制状态、明确 Buy Box 归属或 ERP 活动配置。
-- ============================================================
CREATE TABLE IF NOT EXISTS listing_page_snapshot (
  parent_asin        TEXT NOT NULL,
  shop_account       TEXT NOT NULL,
  site_code          TEXT,
  data               TEXT NOT NULL,
  fetched_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (parent_asin, shop_account)
);
CREATE INDEX IF NOT EXISTS idx_page_snapshot_fetched
  ON listing_page_snapshot(fetched_at);

-- ============================================================
-- 17.2 关键词逐日排名（卡位）
-- 来源：own_keyword_flow（选核心词）+ erp_listing_asin_keyword_rank_history（逐日排名）
-- 用途：判卡位异常 需要 近3天/近7天/逐日 自然排名位（crawNatureRank，越大越靠后）
-- 每 (parent_asin, shop_account, keyword, stat_date) 一条；每天刷一次核心词的历史序列
-- ============================================================
CREATE TABLE IF NOT EXISTS keyword_rank_daily (
  parent_asin   TEXT NOT NULL,
  shop_account  TEXT NOT NULL,
  child_asin    TEXT,
  keyword       TEXT NOT NULL,
  site_code     TEXT,
  stat_date     TEXT NOT NULL,
  nature_rank   INTEGER,                -- 自然排名位 crawNatureRank（越大越靠后）
  sp_rank       INTEGER,                -- 广告排名位 crawSpRank（可空）
  is_core       INTEGER NOT NULL DEFAULT 1,   -- 是否核心词（周搜索量最大）
  data          TEXT,
  fetched_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (parent_asin, shop_account, keyword, stat_date)
);
CREATE INDEX IF NOT EXISTS idx_kwrank_lookup
  ON keyword_rank_daily(parent_asin, shop_account, is_core, stat_date DESC);

-- ============================================================
-- ERP 决策配置（来源：app_db.t_advert_agent_decision_config）
-- 630+ 父ASIN × 42 店铺，含 目标ACOS/预算/产品定位/阶段/淡旺季
-- 每 (parent_asin, shop_id, site_code) 一条；enabled=0 也拉进来但打标记
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_config (
  id             TEXT NOT NULL,             -- ERP UUID
  shop_id        INTEGER NOT NULL,
  parent_asin    TEXT NOT NULL,
  parent_seller_sku TEXT,
  site_code      TEXT,
  day_range      TEXT,
  product_position TEXT,
  product_stage  TEXT,
  season_type    TEXT,
  advert_purposes TEXT,
  target_keyword_types TEXT,
  target_acos_erp   REAL,                   -- ERP 里的整数百分比 ÷100（如 40 → 0.40）
  daily_budget_erp  REAL,                   -- ERP daily_budget_suggest（USD）
  advert_direction_types TEXT,
  enabled        INTEGER DEFAULT 1,
  frequency      TEXT,
  create_time    TEXT,
  update_time    TEXT,
  fetched_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_erp_config_asin ON erp_config(parent_asin, shop_id);
CREATE INDEX IF NOT EXISTS idx_erp_config_shop ON erp_config(shop_id);

-- ============================================================
-- ERP 决策历史（来源：t_advert_agent_decision，取 is_latest=1）
-- 主要为了拿产品名 product_name 和最新一批决策快照
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_decision_latest (
  id              TEXT NOT NULL,
  parent_asin     TEXT NOT NULL,
  shop_id         INTEGER,
  site_code       TEXT,
  product_name    TEXT,
  product_position TEXT,
  product_stage   TEXT,
  season_type     TEXT,
  target_acos_erp REAL,
  daily_budget_erp REAL,
  create_time     TEXT,
  fetched_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_erp_decision_asin ON erp_decision_latest(parent_asin);

-- ============================================================
-- ERP 历史指标快照（来源：t_advert_agent_data_metrics）
-- 数据是 06 月中旬，非滚动源；作为"辅助决策 agent 曾算出的历史值"参考
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_data_metrics (
  id            TEXT NOT NULL,
  decision_id   TEXT NOT NULL,
  metrics_type  TEXT NOT NULL,             -- SUMMARY / DAILY
  day_str       TEXT,
  avg_daily_sale_num REAL,
  acos          REAL,
  organic_order_rate REAL,
  tacos         REAL,
  overall_cvr   REAL,
  cvr           REAL, ctr REAL, cpc REAL, daily_cost REAL,
  create_time   TEXT,
  fetched_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_erp_metrics_dec ON erp_data_metrics(decision_id, metrics_type);

-- ============================================================
-- 系统用户字典（来源：MCP sys_user_query）
-- 只存脱敏字段：id + 用户名 + 登录账号 + 状态
-- 用途：把 asin_owner.principal_user_id / editor_id / creator_id 数字翻译成中文名
-- ============================================================
CREATE TABLE IF NOT EXISTS sys_user (
  id          INTEGER NOT NULL,
  user_name   TEXT,                        -- 显示名，如"张三"
  user_account TEXT,                       -- 登录账号
  user_state  INTEGER,                     -- 1=启用 / 0=停用
  fetched_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (id)
);

-- ============================================================
-- 用户角色（来源：MCP sys_user_query 的 roles 字段）
-- 用途：按角色筛出运营团队。Amazon 运营 = role_code 'GROUP_FBASaler'（FBA销售）
-- ============================================================
CREATE TABLE IF NOT EXISTS user_role (
  user_id     INTEGER NOT NULL,
  role_code   TEXT NOT NULL,
  role_name   TEXT,
  fetched_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (user_id, role_code)
);
CREATE INDEX IF NOT EXISTS idx_user_role_code ON user_role(role_code);

-- ============================================================
-- ASIN 级负责人（产品 × 负责人 映射，一产品可多行）
-- 来源：
--   · 正向 erp_listing_follow_up_by_principal（按负责人拉，含负责+跟进/助理）→ 一产品可对多人
--   · 旧版 az_extend_detail（单负责人，principal→editor→creator）
-- 主键含 principal_user_id：同一 (asin,sku,shop) 允许多个负责人/助理各一行，
-- 使异常按 EXISTS 同时路由到负责人与助理的工作台（重复派单是预期行为）。
-- 优先级链（单行内取显示归属）：principal_user_id → editor_id → creator_id
-- ============================================================
CREATE TABLE IF NOT EXISTS asin_owner (
  asin                TEXT NOT NULL,
  seller_sku          TEXT NOT NULL,
  shop_id             INTEGER,
  shop_account        TEXT,
  site_code           TEXT,
  principal_user_id   INTEGER,             -- ★ 首选：负责人/助理 user_id（正向来源即查询人）
  editor_id           INTEGER,             -- 回退：EDITOR_ID（旧版来源）
  creator_id          INTEGER,             -- 兜底：CREATOR_ID（旧版来源）
  source_update_time  TEXT,                -- MCP 侧 UPDATE_TIME
  fetched_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (asin, seller_sku, shop_id, principal_user_id)
);
CREATE INDEX IF NOT EXISTS idx_asin_owner_asin ON asin_owner(asin);
CREATE INDEX IF NOT EXISTS idx_asin_owner_principal ON asin_owner(principal_user_id);

-- ============================================================
-- 产品指派（运营之间点对点协作）
-- 负责人(assigner) 把自己名下某产品指派给另一运营(assignee) 协助处理。
-- 语义：共享 —— assigner 仍可见可做，assignee 获得对该产品的查看+处理权限。
-- 产品级：一行 = 一个产品(父ASIN+店铺) 指派给一个人；同一产品可派给多人（多行）。
-- 可撤回：删除该行即撤回。
-- ============================================================
CREATE TABLE IF NOT EXISTS task_assignment (
  parent_asin   TEXT NOT NULL,
  shop_account  TEXT NOT NULL,
  assignee_id   INTEGER NOT NULL,             -- 被指派人 sys_user.id
  assigner_id   INTEGER NOT NULL,             -- 指派人 sys_user.id
  note          TEXT,                         -- 指派备注（可选）
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (parent_asin, shop_account, assignee_id)
);
CREATE INDEX IF NOT EXISTS idx_task_assignment_assignee ON task_assignment(assignee_id);
CREATE INDEX IF NOT EXISTS idx_task_assignment_assigner ON task_assignment(assigner_id);

-- 产品访问权限视图：负责人 ∪ 被指派人。
-- 所有"该用户能否看/做某产品"的判断统一查这里，指派对权限的放开在此一处生效。
CREATE VIEW IF NOT EXISTS product_access AS
  SELECT COALESCE(principal_user_id, editor_id, NULLIF(creator_id, -1)) AS user_id,
         asin AS parent_asin, shop_account
    FROM asin_owner
   WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id, -1)) IS NOT NULL
  UNION
  SELECT assignee_id AS user_id, parent_asin, shop_account
    FROM task_assignment;

-- ============================================================
-- 广告目标覆写值（来源：辅助决策 agent 的 MySQL app_db）
--   acos_override.value    → 目标ACOS
--   budget_override.value  → 目标每日预算（USD）
-- 由 ad_state_reader.同步覆写表到本地() 全量拉取；巡检只读本地，不依赖 MySQL 常在线。
-- 一个 (asin, shop_account) 一条；缺记录即"未设置"，判定走"配置待补"。
-- ============================================================
CREATE TABLE IF NOT EXISTS ad_target (
  asin           TEXT NOT NULL,
  shop_account   TEXT NOT NULL DEFAULT '',
  目标ACOS        REAL,                    -- 小数，如 0.30；无则 NULL
  目标每日预算     REAL,                    -- USD；无则 NULL
  源更新时间       TEXT,                    -- MySQL 侧 updated_at（若有）
  fetched_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (asin, shop_account)
);

-- ============================================================
-- 任务处理记录（运营工作台）
--   一个 event_pool.唯一识别 → 多条 action 记录（每次处理/复查/备注都追加一条）
--   最新一条 = 该事件的当前状态；历史条 = 复盘用。
-- ============================================================
CREATE TABLE IF NOT EXISTS task_action (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uid      TEXT NOT NULL,             -- 对应 event_pool.唯一识别
  maintenance_id INTEGER,                   -- 对应一次产品级维护（历史记录可为空）
  user_id        INTEGER,                   -- 操作人 sys_user.id
  action_type    TEXT NOT NULL,             -- '标记处理中'|'完成'|'不处理'|'待复查'|'备注'
  result         TEXT,                      -- '已按建议执行'|'部分执行'|'建议不适用'
  actual_action  TEXT,                      -- 实际动作（自由文本）
  review_at      TEXT,                      -- 复查时间（ISO date）
  notes          TEXT,                      -- 运营备注
  before_metrics TEXT,                      -- JSON snapshot（处理前关键指标）
  after_metrics  TEXT,                      -- JSON snapshot（复查后指标）
  effect         TEXT,                      -- '变好'|'变差'|'待观察'（复查时填）
  created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_task_action_uid ON task_action(event_uid);
CREATE INDEX IF NOT EXISTS idx_task_action_user ON task_action(user_id);
CREATE INDEX IF NOT EXISTS idx_task_action_created ON task_action(created_at);

-- ============================================================
-- 产品级维护与效果观察
-- 一次产品维护覆盖父ASIN+店铺下的多条异常；异常级 task_action 仍保留审计凭证。
-- ============================================================
CREATE TABLE IF NOT EXISTS product_maintenance (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_asin           TEXT NOT NULL,
  shop_account          TEXT NOT NULL,
  user_id               INTEGER,
  result                TEXT,
  actual_action         TEXT,
  notes                 TEXT,
  executed_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  observation_at        TEXT NOT NULL,
  next_inspection_at    TEXT NOT NULL,
  inspection_time_source TEXT NOT NULL DEFAULT 'follow_review',
  status                TEXT NOT NULL DEFAULT '观察中',
  agent_effect          TEXT,
  agent_summary         TEXT,
  agent_observed_at     TEXT,
  agent_payload         TEXT,
  confirmed_at          TEXT,
  confirmed_by          INTEGER,
  confirmation          TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at            TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_product_maintenance_product
  ON product_maintenance(parent_asin, shop_account, executed_at);
CREATE INDEX IF NOT EXISTS idx_product_maintenance_observe
  ON product_maintenance(observation_at, status);

CREATE TABLE IF NOT EXISTS product_maintenance_event (
  maintenance_id        INTEGER NOT NULL REFERENCES product_maintenance(id) ON DELETE CASCADE,
  event_uid             TEXT NOT NULL,
  issue                  TEXT,
  severity               TEXT,
  variant                TEXT,
  baseline_result_id    INTEGER,
  baseline_snapshot     TEXT,
  PRIMARY KEY (maintenance_id, event_uid)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_event_uid
  ON product_maintenance_event(event_uid);

CREATE TABLE IF NOT EXISTS observation_report (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  maintenance_id        INTEGER NOT NULL UNIQUE REFERENCES product_maintenance(id) ON DELETE CASCADE,
  agent_status          TEXT NOT NULL DEFAULT '待观察',
  agent_effect          TEXT,
  confidence            REAL,
  summary               TEXT,
  report_json           TEXT NOT NULL DEFAULT '{}',
  observed_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at            TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_observation_report_status
  ON observation_report(agent_status, observed_at);

-- 应用级一次性数据迁移标记，避免历史兼容逻辑在每次启动时重复覆盖正式状态。
CREATE TABLE IF NOT EXISTS app_migration (
  migration_key TEXT PRIMARY KEY,
  applied_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  details       TEXT
);

-- ============================================================
-- Listing 产品信息（父ASIN 级快照，每日刷）
-- 来源：erp_listing_product_info (azlisting-mcpserver)
-- 用途：给巡检提供 五点/标题/类目/变体主题 等内容完整性字段
-- ============================================================
CREATE TABLE IF NOT EXISTS listing_product_info (
  parent_asin        TEXT NOT NULL,
  parent_seller_sku  TEXT,
  shop_account       TEXT NOT NULL,
  data               TEXT NOT NULL,
  fetched_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  "标题"              TEXT    GENERATED ALWAYS AS (json_extract(data,'$."productName"'))         VIRTUAL,
  "五点1"             TEXT    GENERATED ALWAYS AS (json_extract(data,'$."fiveBulletPoint1"'))    VIRTUAL,
  "五点2"             TEXT    GENERATED ALWAYS AS (json_extract(data,'$."fiveBulletPoint2"'))    VIRTUAL,
  "五点3"             TEXT    GENERATED ALWAYS AS (json_extract(data,'$."fiveBulletPoint3"'))    VIRTUAL,
  "五点4"             TEXT    GENERATED ALWAYS AS (json_extract(data,'$."fiveBulletPoint4"'))    VIRTUAL,
  "五点5"             TEXT    GENERATED ALWAYS AS (json_extract(data,'$."fiveBulletPoint5"'))    VIRTUAL,
  "变体主题"          TEXT    GENERATED ALWAYS AS (json_extract(data,'$."variationThemeName"'))  VIRTUAL,
  "精细度"            TEXT    GENERATED ALWAYS AS (json_extract(data,'$."fineness"'))            VIRTUAL,
  "目标星级"          REAL    GENERATED ALWAYS AS (json_extract(data,'$."targetStarRate"'))      VIRTUAL,
  PRIMARY KEY (parent_asin, shop_account)
);

-- ============================================================
-- 加新字段示例（后续需要提升某个 data 里的原生字段为可查询列时）：
--   ALTER TABLE daily_product_sales ADD COLUMN "新字段名" REAL
--     GENERATED ALWAYS AS (json_extract(data,'$."新字段名"')) VIRTUAL;
--   —— 立即对全部历史行生效，无需回填数据。
-- ============================================================
