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
  PRIMARY KEY (asin, stat_date)
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
  PRIMARY KEY (asin, stat_date)
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
  PRIMARY KEY (asin, parent_asin)
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
-- 加新字段示例（后续需要提升某个 data 里的原生字段为可查询列时）：
--   ALTER TABLE daily_product_sales ADD COLUMN "新字段名" REAL
--     GENERATED ALWAYS AS (json_extract(data,'$."新字段名"')) VIRTUAL;
--   —— 立即对全部历史行生效，无需回填数据。
-- ============================================================
