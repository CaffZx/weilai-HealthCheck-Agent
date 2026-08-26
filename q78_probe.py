"""实际探测爬虫类 MCP 工具，完整 dump 出参字段结构。"""
import json, os, sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/weilai-HealthCheck-Agent-v2.0/.env"), override=True)
from web.backend.deps import get_mcp_client

def dump_struct(obj, prefix="", depth=0, max_depth=6, out=None):
    """递归列出字段：键 + 类型 + 示例值（截断）。"""
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                t = f"{type(v).__name__}[{len(v)}]" if isinstance(v, list) else f"dict[{len(v)}]"
                out.append(f"{'  '*depth}{k}: {t}")
                if isinstance(v, list) and v:
                    dump_struct(v[0], prefix, depth+1, max_depth, out)
                elif isinstance(v, dict):
                    dump_struct(v, prefix, depth+1, max_depth, out)
            else:
                vs = json.dumps(v, ensure_ascii=False)
                out.append(f"{'  '*depth}{k}: {type(v).__name__} = {vs[:80]}")
    elif isinstance(obj, list):
        if obj:
            dump_struct(obj[0], prefix, depth, max_depth, out)

async def main():
    client = get_mcp_client()
    results = {}
    # 1) erp_asin_full_detail：有数据 ASIN
    for asin in ("B0EXAMPLE0", "B0EXAMPLE0"):
        try:
            r = await client.call_tool("erp_asin_full_detail",
                {"asin": asin, "siteCode": "US", "includeReviews": "false"},
                timeout_seconds=90, max_attempts=2)
            lines = [f"== erp_asin_full_detail asin={asin} =="]
            dump_struct(r.raw if isinstance(r.raw, dict) else r.data, out=lines)
            results[f"detail_{asin}"] = {"len": len(json.dumps(r.raw, ensure_ascii=False) if isinstance(r.raw, dict) else ""), "struct": lines}
        except Exception as e:
            results[f"detail_{asin}"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    # 2) erp_listing_review_analysis：评论爬虫（180s 慢）
    try:
        r = await client.call_tool("erp_listing_review_analysis",
            {"asin": "B0EXAMPLE0", "siteCode": "US", "days": 14},
            timeout_seconds=200, max_attempts=1)
        lines = ["== erp_listing_review_analysis asin=B0EXAMPLE0 =="]
        dump_struct(r.raw if isinstance(r.raw, dict) else r.data, out=lines)
        results["review_B0EXAMPLE0"] = {"struct": lines}
    except Exception as e:
        results["review_B0EXAMPLE0"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    with open("/tmp/probe_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("PROBE_DONE", json.dumps({k: ("error" if "error" in v else f"struct_lines={len(v['struct'])}") for k, v in results.items()}, ensure_ascii=False))

import asyncio
asyncio.run(main())
