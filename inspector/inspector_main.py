"""巡检主编排 — 代码判定管线入口。

【管线】
  fixture + local_store 加载数据
  → anomaly_detector (现象即原因型)
  → daily_monitor (表现型滚动指标)
  → severity_grader (S0/S1/S2 判定)
  → score_calculator (执行分 + P0/P1/P2)
  → event_pool (落库)
  → 输出模块7任务卡（前端可直接渲染）

【用法】
  from inspector.inspector_main import 巡检单产品, 巡检批量

  # 单产品巡检（前端逐产品触发）
  结果 = 巡检单产品("key", config_row, mcp_bundle)

  # 批量巡检（定时任务触发）
  结果列表 = 巡检批量(产品列表, 批次号)

【与 LLM 管线的关系】
  LLM 管线：web/backend → llm_judge.judge() → DeepSeek → 前端
  代码管线：web/backend → inspector_main.巡检单产品() → 本地判定 → 前端
  两条管线输出同一 schema，前端同一套 renderJudgment() 渲染。
"""
from __future__ import annotations
import datetime as dt
import logging
import sqlite3
from typing import Any

from data import local_store as store
from data import fixture_loader

from inspector.engine import anomaly_detector as R2
from inspector.engine import severity_grader as R3
from inspector.engine import score_calculator as R4
from inspector.engine import suggestion_picker as R6
from inspector.engine import llm_suggester
from inspector.scheduler import daily_monitor as DM
from inspector.scheduler import event_pool as EP

log = logging.getLogger(__name__)


def _query_owner(parent_asin: str) -> tuple[str, int | None]:
    """从 asin_owner + sys_user 查负责人名称和 ID。优先级：principal → editor → creator。"""
    try:
        with sqlite3.connect(store.DB_PATH) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("""
                SELECT u.user_name, u.id AS user_id
                FROM asin_owner o
                LEFT JOIN sys_user u
                  ON u.id = COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1))
                WHERE o.asin = ? AND u.user_name IS NOT NULL
                ORDER BY o.fetched_at DESC
                LIMIT 1
            """, (parent_asin,)).fetchone()
        if r:
            return r["user_name"], r["user_id"]
    except Exception as e:
        log.warning("_query_owner 查询失败 parent_asin=%s: %s", parent_asin, e)
    return "", None


# -----------------------------------------------------------------------------
# 输出 schema：对齐前端 renderJudgment() 期望的字段
# -----------------------------------------------------------------------------
def _build_task_card(
    *,
    父ASIN: str,
    店铺账号: str,
    站点: str | None,
    产品定位: str | None,
    产品阶段: str | None,
    淡旺季: str | None,
    负责人: str = "",
    负责人ID: int | None = None,
    产品打分: R4.产品打分结果 | R4.观察类结果 | None,
    异常明细: list[dict],
    headline: str = "",
    human_summary: str = "",
    批次号: str | None = None,
) -> dict:
    """构建与前端 renderJudgment() 兼容的任务卡 JSON。"""
    pri_info: dict[str, Any] = {
        "执行优先级": "P2",
        "产品执行分数": 0,
        "处理时限": "7天内处理",
        "父卡严重度": "-",
    }
    if isinstance(产品打分, R4.产品打分结果):
        pri_info = {
            "执行优先级": 产品打分.执行优先级,
            "产品执行分数": 产品打分.产品执行分数,
            "处理时限": 产品打分.处理时限,
            "父卡严重度": 产品打分.父卡严重度,
            "破平次序号": 产品打分.破平次序号,
        }

    loc_info = {
        "店铺站点": f"{店铺账号}/{站点 or '-'}",
        "店铺账号": 店铺账号,
        "站点": 站点 or "-",
        "父ASIN": 父ASIN,
        "负责人": 负责人,
        "负责人ID": 负责人ID,
        "产品定位": 产品定位 or "待补充",
        "产品阶段": 产品阶段 or "待补充",
        "淡旺季": 淡旺季 or "待补充",
    }

    return {
        "来源": "code",
        "优先级信息": pri_info,
        "定位信息": loc_info,
        "headline": headline or "代码判定完成",
        "human_summary": human_summary or "",
        "异常明细": 异常明细,
        "批次号": 批次号,
        "判定时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# -----------------------------------------------------------------------------
# 异常大类映射
# -----------------------------------------------------------------------------
_点位大类: dict[str, tuple[str, str]] = {
    "链接不可售": ("2.1 链接状态", "链接状态"),
    "变体掉线": ("2.1 链接状态", "链接状态"),
    "父子体关系异常": ("2.1 链接状态", "链接状态"),
    "Buy Box丢失": ("2.1 链接状态", "链接状态"),
    "主图异常": ("2.2 内容完整性", "内容完整性"),
    "标题异常": ("2.2 内容完整性", "内容完整性"),
    "五点异常": ("2.2 内容完整性", "内容完整性"),
    "图片异常": ("2.2 内容完整性", "内容完整性"),
    "A+异常": ("2.2 内容完整性", "内容完整性"),
    "变体价差异常": ("2.3 价格与促销", "价格与促销"),
    "促销异常": ("2.3 价格与促销", "价格与促销"),
    "FBA可售库存为0": ("2.4 库存与可售", "库存与可售"),
    "库存不足": ("2.4 库存与可售", "库存与可售"),
    "库存积压": ("2.4 库存与可售", "库存与可售"),
    "滞销异常": ("2.4 库存与可售", "库存与可售"),
    "销量异常": ("2.5 交易表现", "交易表现"),
    "销量增长提示": ("2.5 交易表现", "交易表现"),
    "ACOS异常": ("2.5 交易表现", "交易表现"),
    "广告花费异常": ("2.5 交易表现", "交易表现"),
    "目标偏离": ("2.5 交易表现", "交易表现"),
    "自然流量异常": ("2.5 交易表现", "交易表现"),
    "卡位异常": ("2.5 交易表现", "交易表现"),
    "放量未执行": ("2.5 交易表现", "交易表现"),
    "链接转化率异常下降": ("2.5 交易表现", "交易表现"),
    "合规异常": ("2.6 合规与属性", "合规与属性"),
    "类目异常": ("2.6 合规与属性", "合规与属性"),
    "属性信息异常": ("2.6 合规与属性", "合规与属性"),
    "评分异常": ("2.7 售后", "售后"),
    "退款异常": ("2.7 售后", "售后"),
    "近期集中差评": ("2.7 售后", "售后"),
}


def _lookup_category(问题点位: str) -> tuple[str, str]:
    return _点位大类.get(问题点位, ("", ""))


# -----------------------------------------------------------------------------
# Phase 1: 现象即原因型 — 本地数据可判的检测
# -----------------------------------------------------------------------------
def _detect_with_local_data(
    父ASIN: str,
    快照数据: dict,
    聚合结果: DM.每日聚合结果,
    r2_cfg: dict,
    r3_cfg: dict,
) -> list[R2.命中异常]:
    """用本地库数据跑 anomaly_detector。数据可覆盖的规则都在这里接入。"""
    hits: list[R2.命中异常] = []
    stock = (快照数据 or {}).get("stock") or {}
    product_info = (快照数据 or {}).get("product_info") or {}

    r = R2.detect_FBA可售库存为0(
        FBA可售库存=stock.get("FBA可售库存"),
        子体ASIN=父ASIN, 变体重要性="主要色",
        r2=r2_cfg, r3=r3_cfg,
    )
    if isinstance(r, R2.命中异常):
        hits.append(r)

    r = R2.detect_库存不足(
        FBA可售库存=stock.get("FBA可售库存"),
        过去7天日均销量=聚合结果.过去7天日均销量,
        子体ASIN=父ASIN, 变体重要性="主要色",
        r2=r2_cfg, r3=r3_cfg,
    )
    if isinstance(r, R2.命中异常):
        hits.append(r)

    # ---- 内容完整性（来源：listing_product_info）----
    if product_info:
        # 五点：数 fiveBulletPoint1-5 非空项数；审核状态数据源没有，传 None
        五点项数 = sum(1 for k in ("五点1","五点2","五点3","五点4","五点5")
                      if (product_info.get(k) or "").strip())
        r = R2.detect_五点异常(
            五点内容项数=五点项数, 审核状态=None,
            r2=r2_cfg, r3=r3_cfg,
        )
        if isinstance(r, R2.命中异常):
            hits.append(r)

        # 标题：仅在标题字段空/缺失时命中（审核状态、ERP 基准均无数据源，传 None）
        r = R2.detect_标题异常(
            标题字段=product_info.get("标题"), 审核状态=None, ERP标题基准=None,
            r3=r3_cfg,
        )
        if isinstance(r, R2.命中异常):
            hits.append(r)

    return hits


# -----------------------------------------------------------------------------
# Phase 2+3: 表现型 — daily_monitor → severity_grader
# -----------------------------------------------------------------------------
def _grade_performance_anomalies(
    聚合: DM.每日聚合结果,
    r3_cfg: dict,
) -> list[dict]:
    """用 daily_monitor 聚合结果跑全部 R3 专项判定。"""
    results: list[dict] = []

    def _collect(r, 点位: str):
        """将 severity_grader 结果加入列表。点位优先取结果自带的问题点位。
        广告类点位如缺 目标ACOS/目标每日预算，注入'配置待补'提示条，让运营去辅助决策 agent 设置。"""
        if isinstance(r, R3.严重度判定结果) and r.严重度:
            results.append({
                "问题点位": r.问题点位 or 点位,
                "严重度": r.严重度,
                "分档名称": r.分档名称,
                "判定过程": r.判定过程,
                "命中值": r.命中值,
                "数据快照": getattr(r, "数据快照", []) or [],
            })
        elif isinstance(r, R3.观察类结果) and 点位 in ("ACOS异常", "广告花费异常") \
                and any(k in (r.缺失字段 or []) for k in ("目标ACOS", "目标每日预算")):
            results.append({
                "问题点位": r.问题点位 or 点位,
                "严重度": "配置待补",
                "分档名称": "配置缺失",
                "判定过程": r.原因,
                "命中值": None,
            })

    # 使用 每日聚合结果 提供的按业务分组的便捷方法（daily_monitor.py 里定义）
    # 加新专项：daily_monitor 加一个 to_XXX_kwargs()，这里加一行 _collect(...)
    _collect(R3.判ACOS异常(**聚合.to_ACOS异常_kwargs(), 参数=r3_cfg), "ACOS异常")
    _collect(R3.判广告花费异常(**聚合.to_广告花费异常_kwargs(), 参数=r3_cfg), "广告花费异常")
    _collect(R3.判销量类异常(**聚合.to_销量类异常_kwargs(), 参数=r3_cfg), "销量异常")
    _collect(R3.判目标偏离(**聚合.to_目标偏离_kwargs(), 参数=r3_cfg), "目标偏离")
    _collect(R3.判自然流量异常(**聚合.to_自然流量异常_kwargs(), 参数=r3_cfg), "自然流量异常")
    _collect(R3.判卡位异常(**聚合.to_卡位异常_kwargs(), 参数=r3_cfg), "卡位异常")
    _collect(R3.判评分异常(**聚合.to_评分异常_kwargs(), 参数=r3_cfg), "评分异常")
    _collect(R3.判退款异常(**聚合.to_退款异常_kwargs(), 参数=r3_cfg), "退款异常")
    _collect(R3.判库存积压(**聚合.to_库存积压_kwargs(), 参数=r3_cfg), "库存积压")
    _collect(R3.判放量未执行(**聚合.to_放量未执行_kwargs(), 参数=r3_cfg), "放量未执行")
    _collect(R3.判链接转化率异常(**聚合.to_链接转化率异常_kwargs(), 参数=r3_cfg), "链接转化率异常下降")
    _collect(R3.判滞销异常(**聚合.to_滞销异常_kwargs(), 参数=r3_cfg), "滞销异常")

    return results


# -----------------------------------------------------------------------------
# 主入口：巡检单产品
# -----------------------------------------------------------------------------
def 巡检单产品(
    key: str,
    config_row: dict,
    mcp_bundle: dict | None = None,
    *,
    批次号: str | None = None,
    r2_cfg: dict | None = None,
    r3_cfg: dict | None = None,
    r4_cfg: dict | None = None,
) -> dict:
    """对单个产品跑完整代码巡检管线 → 前端兼容的任务卡 JSON。"""
    父ASIN = config_row.get("parent_asin", "")
    店铺账号 = config_row.get("shop_account", "")
    站点 = config_row.get("site_code", "")

    if r2_cfg is None:
        r2_cfg = R2.加载R2()
    if r3_cfg is None:
        r3_cfg = R3.加载参数()
    if r4_cfg is None:
        r4_cfg = R4.加载参数()

    # Phase 0: 加载数据
    销售数据 = store.query_parent_daily_sales(父ASIN, days=30)
    快照数据 = store.query_product_snapshots(父ASIN, 店铺账号) or {}

    聚合 = DM.聚合单产品(
        父ASIN=父ASIN, 店铺账号=店铺账号, 站点=站点 or "",
        销售数据=销售数据, 快照数据=快照数据,
    )

    # Phase 1: 现象即原因型
    现象命中 = _detect_with_local_data(父ASIN, 快照数据, 聚合, r2_cfg, r3_cfg)

    # Phase 2+3: 表现型
    表现判定 = _grade_performance_anomalies(聚合, r3_cfg)

    # ---- 汇总 R4 异常输入 ----
    # 设计约定：数据不足（严重度为 None）→ 不进入打分（走观察类），绝不兜底成 S2
    # 作用层级：一律走 R2.查作用层级（真相源在 yaml，代码不硬编码）
    全部异常输入: list[R4.异常输入] = []
    跳过打分: list[dict] = []          # 记录未参与打分的异常（数据不足或层级未知）
    for h in 现象命中:
        if not h.默认严重度:
            跳过打分.append({"问题点位": h.问题点位, "原因": "严重度取值失败（R3默认表无对应值）"})
            continue
        全部异常输入.append(R4.异常输入(
            问题点位=h.问题点位,
            严重度=h.默认严重度,
            作用层级=h.作用层级,
            变体重要性=h.变体重要性,
            命中对象=h.命中变体,
        ))
    for g in 表现判定:
        if not g["严重度"]:
            跳过打分.append({"问题点位": g["问题点位"], "原因": "严重度为空"})
            continue
        # "配置待补"是运营配置提示，不是异常，仅走前端明细不进打分/事件池
        if g["严重度"] == "配置待补":
            跳过打分.append({"问题点位": g["问题点位"], "原因": "配置待补（不进打分）"})
            continue
        层级 = R2.查作用层级(g["问题点位"], r2_cfg)
        if 层级 is None:
            跳过打分.append({"问题点位": g["问题点位"], "原因": "R2 未定义该点位的作用层级"})
            continue
        全部异常输入.append(R4.异常输入(
            问题点位=g["问题点位"],
            严重度=g["严重度"],
            作用层级=层级,
            变体重要性=None,     # 表现型自身为父体级汇总数据，无变体重要性
            命中对象=None,
        ))

    # Phase 4: R4 打分
    定位 = config_row.get("product_position", "")
    阶段 = config_row.get("product_stage", "")
    淡旺季 = config_row.get("season_type", "")
    产品打分 = None
    if 全部异常输入:
        标签 = R4.产品标签(
            产品定位_ERP值=定位, 产品阶段_ERP值=阶段, 淡旺季_ERP值=淡旺季,
        )
        产品打分 = R4.算产品分数(全部异常输入, 标签, r4_cfg)

    # Phase 5: 落库事件池（严重度为空 → 跳过落库；作用层级从 R2 查权威值）
    # 从 Phase 4 的 产品打分 反查每条 (点位, 严重度) → 单异常执行分数
    score_map: dict[tuple[str, str], float] = {}
    if isinstance(产品打分, R4.产品打分结果):
        for r in [产品打分.最高单异常] + 产品打分.其余异常:
            score_map[(r.问题点位, r.严重度)] = r.单异常执行分数

    for h in 现象命中:
        if not h.默认严重度:
            continue
        _upsert_event(h.问题点位, h.默认严重度, h.作用层级,
                      "现象即原因型", h.命中变体, h.变体重要性, h.命中依据,
                      父ASIN, 店铺账号, 站点, 批次号,
                      单异常执行分数=score_map.get((h.问题点位, h.默认严重度)))
    for g in 表现判定:
        if not g["严重度"] or g["严重度"] == "配置待补":
            continue    # 配置提示不是异常，不进事件池
        层级 = R2.查作用层级(g["问题点位"], r2_cfg) or "链接级"
        _upsert_event(g["问题点位"], g["严重度"], 层级,
                      "表现型", None, None, g.get("判定过程", ""),
                      父ASIN, 店铺账号, 站点, 批次号,
                      单异常执行分数=score_map.get((g["问题点位"], g["严重度"])))

    # Phase 6: 构建 异常明细
    异常明细 = _build_anomaly_details(
        现象命中, 表现判定, 产品打分,
        product_context={"parent_asin": 父ASIN, "shop_account": 店铺账号, "site": 站点},
    )

    # headline & summary
    if isinstance(产品打分, R4.产品打分结果):
        headline = (
            f"{产品打分.执行优先级} · 执行分 {产品打分.产品执行分数} · "
            f"父卡严重度 {产品打分.父卡严重度} · {len(异常明细)} 项异常"
        )
        严重项 = [a for a in 异常明细 if a.get("该条严重度") in ("S0", "S1")]
        human_summary = "；".join(
            f"「{a['问题点位']}」{a['该条严重度']}" for a in 严重项[:5]
        ) if 严重项 else f"共 {len(异常明细)} 项异常，均为 S2 轻微"
    elif 异常明细:
        配置待补项 = [a for a in 异常明细 if a.get("该条严重度") == "配置待补"]
        真异常项 = [a for a in 异常明细 if a.get("该条严重度") != "配置待补"]
        if 配置待补项 and not 真异常项:
            # 全是配置提示，不是真异常
            headline = f"仅 {len(配置待补项)} 项配置待补提示，未识别真实异常"
            human_summary = "；".join(
                f"「{a['问题点位']}」请在辅助决策 agent 设置目标值" for a in 配置待补项[:5]
            )
        else:
            headline = f"检测到 {len(真异常项)} 项异常（标签数据不足，未打分）"
            if 配置待补项:
                headline += f"，另有 {len(配置待补项)} 项配置待补"
            human_summary = ""
    else:
        headline = "未检测到异常"
        human_summary = ""

    负责人名, 负责人ID = _query_owner(父ASIN)
    return _build_task_card(
        父ASIN=父ASIN, 店铺账号=店铺账号, 站点=站点,
        产品定位=定位, 产品阶段=阶段, 淡旺季=淡旺季,
        负责人=负责人名, 负责人ID=负责人ID,
        产品打分=产品打分, 异常明细=异常明细,
        headline=headline, human_summary=human_summary, 批次号=批次号,
    )


def _build_anomaly_details(
    现象命中: list[R2.命中异常],
    表现判定: list[dict],
    产品打分: R4.产品打分结果 | R4.观察类结果 | None,
    *,
    product_context: dict | None = None,
) -> list[dict]:
    """构建前端 异常明细 列表，含该条执行分数。

    建议生成策略：
      1. 优先：LLM 批量生成（结合知识库话术）
      2. 回退：R6 YAML 模板（LLM 不可用时）
    """
    # (点位, 严重度) → 单异常执行分数
    score_map: dict[tuple[str, str], float] = {}
    if isinstance(产品打分, R4.产品打分结果):
        for r in [产品打分.最高单异常] + 产品打分.其余异常:
            score_map[(r.问题点位, r.严重度)] = r.单异常执行分数

    # ---- 构建 LLM 输入用的异常原始数据列表 ----
    anomaly_data: list[dict] = []
    for h in 现象命中:
        anomaly_data.append({
            "问题点位": h.问题点位,
            "严重度": h.默认严重度 or "数据不足",
            "作用层级": h.作用层级,
            "命中变体": h.命中变体 or "",
            "变体重要性": h.变体重要性 or "",
            "命中依据": h.命中依据 or "",
            "类型": "现象即原因型",
        })
    for g in 表现判定:
        sev = g["严重度"]
        anomaly_data.append({
            "问题点位": g["问题点位"],
            "严重度": sev,
            "作用层级": "链接级",
            "命中变体": "",
            "变体重要性": "",
            "命中依据": g.get("判定过程", ""),
            "判定过程": g.get("判定过程", ""),
            "命中值": g.get("命中值"),
            "分档名称": g.get("分档名称", ""),
            "类型": "表现型",
        })

    # ---- 尝试 LLM 批量生成建议 ----
    llm_results = llm_suggester.suggest_batch(anomaly_data, product_context) if anomaly_data else []

    # ---- 逐条构建异常明细 ----
    details: list[dict] = []
    llm_idx = 0  # LLM 结果游标

    for h in 现象命中:
        大类, 类型 = _lookup_category(h.问题点位)
        sev = h.默认严重度

        # LLM 建议（优先）→ R6 YAML（回退）→ 原始字段（兜底）
        llm_r = llm_results[llm_idx] if llm_results and llm_idx < len(llm_results) else None
        r6_r = R6.选取(h) if llm_r is None else None
        llm_idx += 1

        details.append({
            "该条严重度": sev or "数据不足",
            "该条执行分数": score_map.get((h.问题点位, sev), -1) if sev else -1,
            "异常类型": 类型, "异常大类": 大类,
            "问题点位": h.问题点位, "作用层级": h.作用层级,
            "命中变体": h.命中变体 or "", "变体重要性": h.变体重要性 or "",
            "异常状态": "新发现",
            "具体表现": (llm_r or r6_r or {}).get("具体表现") or h.问题点位,
            "判断依据": (llm_r or r6_r or {}).get("判断依据") or h.命中依据,
            "处理建议": (llm_r or r6_r or {}).get("处理建议") or "待补充",
            "初步原因": "不适用",
            "技术依据": h.命中依据,
            "上次处理记录": "无", "复查要求": "待补充",
        })

    for g in 表现判定:
        大类, 类型 = _lookup_category(g["问题点位"])
        sev = g["严重度"]
        _配置缺 = sev == "配置待补"

        llm_r = llm_results[llm_idx] if llm_results and llm_idx < len(llm_results) else None
        r6_r = R6.选取表现型(g) if llm_r is None else None
        llm_idx += 1

        # 数据快照 → "技术依据"（结构化数据点，跟判据结论互补，不重复）
        snap = g.get("数据快照") or []
        details.append({
            "该条严重度": sev,
            "该条执行分数": score_map.get((g["问题点位"], sev), -1),
            "异常类型": "配置缺失" if _配置缺 else 类型,
            "异常大类": 大类,
            "问题点位": g["问题点位"], "作用层级": "链接级",
            "命中变体": "", "变体重要性": "",
            "异常状态": "新发现",
            "具体表现": ("目标配置缺失" if _配置缺
                     else (llm_r or r6_r or {}).get("具体表现") or g["问题点位"]),
            "判断依据": (llm_r or r6_r or {}).get("判断依据") or g.get("判定过程", ""),
            "处理建议": ("请在辅助决策 agent 补齐目标配置后重新巡检" if _配置缺
                     else (llm_r or r6_r or {}).get("处理建议") or "待补充"),
            "初步原因": ("未设置广告目标/广告目标不完整" if _配置缺 else "不适用"),
            # 技术依据回落到 判定过程（无快照的场景）
            "技术依据": g.get("判定过程", ""),
            # 数据快照 —— 前端优先渲染这个（结构化）；无则回落到技术依据
            "数据快照": snap,
            "上次处理记录": "无", "复查要求": "待补充",
        })

    details.sort(key=lambda a: a["该条执行分数"], reverse=True)
    return details


def _upsert_event(
    问题点位: str, 严重度: str, 作用层级: str, 异常类型: str,
    命中变体: str | None, 变体重要性: str | None, 判定依据: str,
    父ASIN: str, 店铺账号: str, 站点: str | None, 批次号: str | None,
    *, 单异常执行分数: float | None = None,
) -> bool:
    """落库到 event_pool；失败即 warning 上报，返回 False 供调用方统计。"""
    try:
        大类, _ = _lookup_category(问题点位)
        EP.处理命中事件(EP.事件命中输入(
            店铺账号=店铺账号, 父ASIN=父ASIN, 问题点位=问题点位,
            作用层级=作用层级, 异常类型=异常类型, 严重度=严重度,
            判定依据={"判定过程": 判定依据, "触发字段": {}, "判定日志": {"批次号": 批次号}},
            站点=站点, 异常大类=大类, 命中变体=命中变体, 变体重要性=变体重要性,
            单异常执行分数=单异常执行分数, 参数版本="code-v1",
        ))
        return True
    except Exception as e:
        log.warning("事件落库失败 父ASIN=%s 点位=%s 严重度=%s 原因=%s",
                    父ASIN, 问题点位, 严重度, e)
        return False


# -----------------------------------------------------------------------------
# 批量巡检
# -----------------------------------------------------------------------------
def 巡检批量(
    产品列表: list[dict] | None = None,
    批次号: str | None = None,
) -> list[dict]:
    """批量巡检，返回任务卡列表（按产品执行分数降序）。"""
    if 批次号 is None:
        批次号 = dt.datetime.now().strftime("%Y%m%d-%H%M")

    if 产品列表 is None:
        configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
        smap = fixture_loader.load_shop_map()
        产品列表 = []
        for key in fixture_loader.list_keys():
            cfg = dict(configs.get(key, {}))
            shop = smap.get(cfg.get("shop_id", ""), {})
            cfg["shop_account"] = shop.get("account", "")
            产品列表.append({"key": key, "config": cfg, "mcp_bundle": None})

    r2_cfg = R2.加载R2()
    r3_cfg = R3.加载参数()
    r4_cfg = R4.加载参数()

    结果: list[dict] = []
    log.info("开始批量巡检 %d 个产品 批次=%s", len(产品列表), 批次号)

    for i, p in enumerate(产品列表):
        try:
            card = 巡检单产品(
                key=p["key"], config_row=p["config"], mcp_bundle=p.get("mcp_bundle"),
                批次号=批次号, r2_cfg=r2_cfg, r3_cfg=r3_cfg, r4_cfg=r4_cfg,
            )
            结果.append(card)
        except Exception as e:
            log.warning("产品 %s 巡检失败: %s", p["key"], e)
            结果.append({
                "来源": "code", "优先级信息": {"执行优先级": "P2", "产品执行分数": 0},
                "定位信息": {}, "headline": f"巡检异常: {e}", "human_summary": "", "异常明细": [],
            })

        if (i + 1) % 20 == 0:
            log.info("  进度: %d/%d", i + 1, len(产品列表))

    结果.sort(key=lambda c: c.get("优先级信息", {}).get("产品执行分数", 0), reverse=True)
    log.info("批量巡检完成: %d 个产品", len(结果))
    return 结果


# -----------------------------------------------------------------------------
# 自检
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    store.init_db()

    print("=" * 70)
    print("inspector_main 自检 · 代码巡检管线")
    print("=" * 70)

    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()

    # 单产品巡检
    keys = fixture_loader.list_keys()
    test_key = keys[0]
    cfg = dict(configs.get(test_key, {}))
    shop = smap.get(cfg.get("shop_id", ""), {})
    cfg["shop_account"] = shop.get("account", "")

    print(f"\n测试产品: {test_key}")
    print(f"  父ASIN: {cfg.get('parent_asin')}")
    print(f"  店铺: {cfg.get('shop_account')}")
    print(f"  定位: {cfg.get('product_position')} / 阶段: {cfg.get('product_stage')} / 淡旺季: {cfg.get('season_type')}")

    card = 巡检单产品(key=test_key, config_row=cfg)
    print(f"\nheadline: {card['headline']}")
    print(f"human_summary: {card['human_summary']}")
    print(f"优先级: {card['优先级信息']['执行优先级']} (分数: {card['优先级信息']['产品执行分数']})")
    print(f"异常明细: {len(card['异常明细'])} 项")
    for a in card['异常明细']:
        print(f"  [{a['该条严重度']}] {a['问题点位']} ({a['异常类型']}) — {a['判断依据'][:80]}")

    # 批量抽样
    print(f"\n--- 批量巡检抽样 (5 产品) ---")
    抽样 = []
    for key in keys[:5]:
        cfg = dict(configs.get(key, {}))
        shop = smap.get(cfg.get("shop_id", ""), {})
        cfg["shop_account"] = shop.get("account", "")
        抽样.append({"key": key, "config": cfg, "mcp_bundle": None})

    批量结果 = 巡检批量(抽样, 批次号="test-batch")
    for card in 批量结果:
        pri = card["优先级信息"]
        print(f"  {card['定位信息'].get('店铺站点','')} | {pri['执行优先级']} {pri['产品执行分数']}分 | {card['headline'][:80]}")
