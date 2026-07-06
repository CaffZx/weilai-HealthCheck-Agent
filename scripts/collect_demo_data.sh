#!/usr/bin/env bash
# 采集 70 个 ASIN 的 MCP + ERP 数据，作为知识库测试的固定样本
# 运行位置：服务器 (36.140.52.167)，需要能访问 ERP / StarRocks / 内网 MCP
set -euo pipefail

OUT=${1:-/tmp/demo_fixtures}
mkdir -p "$OUT"/{configs,listing,sales,campaigns,search_term,placement}

ERP_ARGS=(-h10.0.0.0 -P3306 -uapp_db -pWAzx\)erp2312whIT666\& app_db)
SR_ARGS=(-h10.0.0.0 -P9030 -ucbr -pcbr123 app_db)
MCP=http://10.0.0.0:7089/mcp
KEY=REMOVED_API_KEY
END_DATE=$(date +%F)
START_DATE=$(date -d '30 days ago' +%F 2>/dev/null || date -v-30d +%F)

mysql() { docker run --rm -i --network host mysql:8.0 mysql --default-character-set=utf8mb4 "$@"; }

echo "[1/5] 挑 70 个不同 ASIN 的 config 行"
mysql "${ERP_ARGS[@]}" -N --batch -e "
SELECT id, shop_id, parent_asin, parent_seller_sku, site_code, product_position, product_stage,
       season_type, advert_purposes, target_keyword_types, target_acos_suggest, daily_budget_suggest
FROM t_advert_agent_decision_config
WHERE enabled=1 AND parent_asin IS NOT NULL AND parent_seller_sku IS NOT NULL
  AND id IN (
    SELECT MIN(id) FROM t_advert_agent_decision_config
    WHERE enabled=1 AND parent_asin IS NOT NULL AND parent_seller_sku IS NOT NULL
    GROUP BY parent_asin
  )
ORDER BY RAND()
LIMIT 70;" > "$OUT/configs/rows.tsv"
wc -l "$OUT/configs/rows.tsv"

echo "[2/5] 建 shop_id → shop_account 映射"
IDS=$(cut -f2 "$OUT/configs/rows.tsv" | sort -u | paste -sd, -)
mysql "${SR_ARGS[@]}" -N --batch -e "
SELECT id, account, platform_site_code FROM dwd_shop WHERE id IN ($IDS);" > "$OUT/configs/shop_map.tsv"
wc -l "$OUT/configs/shop_map.tsv"

echo "[3/5] 初始化 MCP 会话"
SID=$(curl -sS -D - -X POST "$MCP" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "X-Api-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}' \
  2>&1 | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r\n')
curl -sS -X POST "$MCP" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "X-Api-Key: $KEY" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
echo "SID=$SID"

call_mcp() {
  local tool=$1 args=$2
  curl -sS -X POST "$MCP" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "X-Api-Key: $KEY" -H "Mcp-Session-Id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

echo "[4/5] 逐行调 MCP"
N=0
while IFS=$'\t' read -r id shop_id parent_asin parent_sku site_code position stage season purposes kw_types acos budget; do
  N=$((N+1))
  # 取 shop_account
  account=$(awk -F'\t' -v k="$shop_id" '$1==k{print $2}' "$OUT/configs/shop_map.tsv")
  [ -z "$account" ] && { echo "  #$N SKIP shop_id=$shop_id no account"; continue; }
  key="${parent_asin}__${shop_id}"
  echo "  #$N $account $parent_asin"

  base="{\"shop_account\":\"$account\",\"parent_asin\":\"$parent_asin\",\"parent_seller_sku\":\"$parent_sku\"}"
  sales="{\"shop_account\":\"$account\",\"parent_asin\":\"$parent_asin\",\"parent_seller_sku\":\"$parent_sku\",\"start_date\":\"$START_DATE\",\"end_date\":\"$END_DATE\"}"

  call_mcp listing_basic_info_v2   "$base"  > "$OUT/listing/$key.json"
  call_mcp product_sales           "$sales" > "$OUT/sales/$key.json"
  call_mcp ad_campaign_list        "$base"  > "$OUT/campaigns/$key.json"
done < "$OUT/configs/rows.tsv"

echo "[5/5] 汇总"
{
  echo "asin_count: $(ls "$OUT/listing" | wc -l)"
  echo "collected_at: $(date -Iseconds)"
  echo "date_window: $START_DATE ~ $END_DATE"
} > "$OUT/README.txt"
cat "$OUT/README.txt"
