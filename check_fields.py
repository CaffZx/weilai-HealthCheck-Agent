from dotenv import load_dotenv
import os, json
from sqlalchemy import create_engine, text

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")
eng = create_engine(os.environ["PATROL_DATABASE_URL"])

with eng.connect() as c:
    # response_json 顶层结构
    print("=== response_json 顶层 keys ===")
    r = c.execute(text(
        "SELECT JSON_KEYS(response_json) AS ks FROM t_patrol_raw_fact "
        "WHERE tool_name = 'erp_asin_full_detail' AND response_json IS NOT NULL LIMIT 1"
    )).mappings().first()
    print("  ", r["ks"] if r else "无")

    # content 里 text 的结构（提取 linkStatus）
    print("=== response_json 里 linkStatus 相关（字符串搜索）===")
    r = c.execute(text(
        "SELECT SUBSTRING(response_json, 1, 2000) AS s FROM t_patrol_raw_fact "
        "WHERE tool_name = 'erp_asin_full_detail' AND response_json IS NOT NULL LIMIT 1"
    )).mappings().first()
    s = r["s"] if r else ""
    # 找 linkStatus 附近内容
    idx = s.find("linkStatus")
    if idx >= 0:
        print("  linkStatus 附近:", s[max(0, idx-50):idx+300])
    else:
        print("  （前2000字符里无 linkStatus，可能是 JSON 嵌套在其他层）")
        # 找 coupon
        idx2 = s.find("coupon")
        print("  coupon 位置:", idx2, " 附近:", s[max(0,idx2-80):idx2+200] if idx2>=0 else "无")
