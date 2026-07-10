"""每日聚合器 — 从本地 SQLite 计算滚动窗口指标，输出结构化数据喂给 severity_grader。

【角色】数据聚合层，不做判定
  1. 从 local_store 读取原始销售数据 + 快照数据
  2. 计算近N天滚动均值 / 连续偏离天数 / 自然占比 / 周环比 / 月进度
  3. 输出 每日聚合结果 dataclass → 直接解包传给 severity_grader 各专项函数

【设计约定】
  - 命名复用 MCP 原生字段名（与 local_store 生成列一致）
  - 数据不足 → 字段为 None，缺失字段列表标注
  - 不读 yaml、不做判定 — 只给 severity_grader 提供干净的聚合输入
  - 一个产品一个聚合结果

【用法】
    from inspector.scheduler.daily_monitor import 批量聚合, 聚合单产品
    全部结果 = 批量聚合()                         # 所有 70 个产品
    单个结果 = 聚合单产品("B0EXAMPLE0", "am_example_us", "UK")
"""
from __future__ import annotations
import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from data import local_store as store

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 数据契约
# -----------------------------------------------------------------------------
@dataclass
class 每日聚合结果:
    """一个父ASIN的所有性能指标聚合，字段名对齐 severity_grader 各专项函数的参数名。
    可直接 **dataclasses.asdict() 或手动解包传入判*函数。"""

    # ---- 标识 ----
    父ASIN: str
    店铺账号: str
    站点: str

    # ---- 销量（判销量类异常 / 判目标偏离 / 判放量未执行 / 判库存积压）----
    近3天日均销量: float | None = None          # 判销量类异常.近3天日均销量
    过去7天日均销量: float | None = None         # 判销量类异常.过去7天日均销量
    近30天日均销量: float | None = None          # 判库存积压.近30天日均销量
    近7天逐日单量: list[float] = field(default_factory=list)  # 判目标偏离.S0升级
    销量连续下降天数: int = 0                    # 判销量类异常.连续天数（<近7天均的天数）

    # ---- ACOS（判ACOS异常 / 判放量未执行 共用同一份指标）----
    近7天平均ACOS: float | None = None           # 判ACOS异常 + 判放量未执行 共用
    近3天平均ACOS: float | None = None           # 判ACOS异常
    近3天累计广告花费: float | None = None       # 判ACOS异常.样本量
    近7天日均花费: float | None = None           # 判ACOS异常.样本量（= 近7天平均花费但语义为"日均"）
    近3天累计点击: int | None = None             # 判ACOS异常.样本量（暂无点击数据源）
    ACOS连续超标天数: int = 0                    # 判ACOS异常.连续天数

    # ---- 广告花费（判广告花费异常 + 判放量未执行 共用同一份指标）----
    近7天平均花费: float | None = None           # 判广告花费异常 + 判放量未执行 共用
    近3天平均花费: float | None = None           # 判广告花费异常 + 判放量未执行 共用
    花费连续偏离天数: int = 0                    # 判广告花费异常.连续天数

    # ---- 目标偏离（判目标偏离）----
    近3天日均单量: float | None = None           # 判目标偏离.近3天日均单量
    日均目标单量: float | None = None             # 判目标偏离.日均目标单量
    月累计完成率: float | None = None             # 判目标偏离.月累计完成率 (0-1)
    月时间进度: float | None = None               # 判目标偏离.月时间进度 (0-1)

    # ---- 自然流量（判自然流量异常 + 判放量未执行 共用）----
    # 自然单量 = 全部单量 - 广告单量；占比 = 自然单量 / 全部单量
    近3天平均自然占比: float | None = None       # 判自然流量异常 + 判放量未执行 共用
    近7天平均自然占比: float | None = None       # 判自然流量异常 + 判放量未执行 共用
    前7天平均自然占比: float | None = None       # 判放量未执行（再往前 7 天，第 8~14 天）
    近3天自然订单数: float | None = None         # 判自然流量异常
    近7天自然订单数: float | None = None         # 判自然流量异常
    近7天逐日自然占比: list[float] = field(default_factory=list)  # 判自然流量异常 S0 升级

    # ---- 卡位（判卡位异常）----
    # 数据缺口：暂无逐日卡位数据（仅 listing_baseline 快照有大类排名）
    近3天平均卡位: float | None = None
    近7天平均卡位: float | None = None
    近7天逐日卡位: list[float] = field(default_factory=list)

    # ---- 转化率（判链接转化率异常，周维度）----
    本周转化率: float | None = None               # = listing_baseline.链接转化率（快照值）
    上周转化率: float | None = None               # 无上周数据 → None
    类目平均转化率: float | None = None           # = listing_baseline.类目转化率
    本周会话: int | None = None                   # 数据缺口 → None
    上周会话: int | None = None                   # 数据缺口 → None
    本周订单商品总数: int | None = None           # 数据缺口 → None
    上周订单商品总数: int | None = None           # 数据缺口 → None

    # ---- 评分（判评分异常）----
    当前评分: float | None = None                 # = listing_baseline.星级
    近7天平均评分: float | None = None            # 暂无逐日评分历史 → = 当前评分（快照）
    目标评分: float | None = None                 # = product_tags.TARGET_STAR_RATE

    # ---- 退款（判退款异常）----
    本周退款率: float | None = None               # = listing_baseline.16周退款率（快照值）
    上周退款率: float | None = None               # 无上周数据 → = listing_baseline.32周退款率（作为参考）
    类目平均退换货率: float | None = None         # = listing_baseline.类目退换货率

    # ---- 放量未执行 ----
    目标ACOS: float | None = None                 # 判放量未执行.目标ACOS（从ERP标签）

    # ---- 库存积压 / 滞销（判库存积压 / 判滞销异常）----
    可售库存: float | None = None                 # = stock_summary.FBA可售库存
    未来N月目标累计_覆盖月数: float | None = None  # 判库存积压.B档
    是否新品观察期: bool = False
    有效销售天数: int = 0                         # 有销售记录的天数
    # 滞销（数据缺口）
    有滞销库存: bool = False
    命中子ASIN滞销库存: float | None = None       # 数据缺口 → None
    命中子ASIN近30天日均销量: float | None = None # 数据缺口 → None
    父ASIN汇总单月超龄仓租费用: float | None = None  # 数据缺口 → None

    # ---- 元信息 ----
    数据窗口_天数: int = 0
    数据窗口_起: str | None = None               # YYYY-MM-DD
    数据窗口_止: str | None = None
    缺失字段: list[str] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # 便捷方法：按 R3 专项分组导出 kwargs
    # 用法：R3.判ACOS异常(**聚合.to_ACOS异常_kwargs(), 参数=cfg)
    # 加新专项时只需在这里加一个方法，字段仍集中在类顶部，业务边界清晰。
    # -------------------------------------------------------------------------
    def to_ACOS异常_kwargs(self) -> dict:
        return {
            "近7天平均ACOS": self.近7天平均ACOS,
            "近3天平均ACOS": self.近3天平均ACOS,
            "连续天数": self.ACOS连续超标天数,
            "近3天累计广告花费": self.近3天累计广告花费,
            "近7天日均花费": self.近7天日均花费,
            "近3天累计点击": self.近3天累计点击,
        }

    def to_广告花费异常_kwargs(self) -> dict:
        return {
            "近7天平均花费": self.近7天平均花费,
            "近3天平均花费": self.近3天平均花费,
            "连续天数": self.花费连续偏离天数,
        }

    def to_销量类异常_kwargs(self) -> dict:
        return {
            "过去7天日均销量": self.过去7天日均销量,
            "近3天日均销量": self.近3天日均销量,
            "连续天数": self.销量连续下降天数,
        }

    def to_目标偏离_kwargs(self) -> dict:
        return {
            "近3天日均单量": self.近3天日均单量,
            "日均目标单量": self.日均目标单量,
            "近7天逐日单量": self.近7天逐日单量,
            "月累计完成率": self.月累计完成率,
            "月时间进度": self.月时间进度,
        }

    def to_自然流量异常_kwargs(self) -> dict:
        return {
            "近3天平均自然占比": self.近3天平均自然占比,
            "近7天平均自然占比": self.近7天平均自然占比,
            "近3天自然订单数": self.近3天自然订单数,
            "近7天自然订单数": self.近7天自然订单数,
            "近7天逐日自然占比": self.近7天逐日自然占比,
        }

    def to_卡位异常_kwargs(self) -> dict:
        return {
            "近3天平均卡位": self.近3天平均卡位,
            "近7天平均卡位": self.近7天平均卡位,
            "近7天逐日卡位": self.近7天逐日卡位,
        }

    def to_链接转化率异常_kwargs(self) -> dict:
        return {
            "本周转化率": self.本周转化率,
            "上周转化率": self.上周转化率,
            "类目平均转化率": self.类目平均转化率,
            "本周会话": self.本周会话,
            "上周会话": self.上周会话,
            "本周订单商品总数": self.本周订单商品总数,
            "上周订单商品总数": self.上周订单商品总数,
        }

    def to_评分异常_kwargs(self) -> dict:
        return {
            "当前评分": self.当前评分,
            "近7天平均评分": self.近7天平均评分,
            "目标评分": self.目标评分,
        }

    def to_退款异常_kwargs(self) -> dict:
        return {
            "本周退款率": self.本周退款率,
            "上周退款率": self.上周退款率,
            "类目平均退换货率": self.类目平均退换货率,
            "本周订单商品总数": self.本周订单商品总数,
            "上周订单商品总数": self.上周订单商品总数,
        }

    def to_放量未执行_kwargs(self) -> dict:
        return {
            "近7天日均销量": self.过去7天日均销量,
            "日均目标单量": self.日均目标单量,
            "可售库存": self.可售库存,
            "近7天平均ACOS": self.近7天平均ACOS,
            "目标ACOS": self.目标ACOS,
            "近3天平均花费": self.近3天平均花费,
            "近7天平均花费": self.近7天平均花费,
            "近7天平均自然占比": self.近7天平均自然占比,
            "前7天平均自然占比": self.前7天平均自然占比,
            "近3天平均自然占比": self.近3天平均自然占比,
        }

    def to_库存积压_kwargs(self) -> dict:
        return {
            "可售库存": self.可售库存,
            "近30天日均销量": self.近30天日均销量,
            "未来N月目标累计_覆盖月数": self.未来N月目标累计_覆盖月数,
            "是否新品观察期": self.是否新品观察期,
            "有效销售天数": self.有效销售天数,
        }

    def to_滞销异常_kwargs(self) -> dict:
        return {
            "有滞销库存": self.有滞销库存,
            "命中子ASIN滞销库存": self.命中子ASIN滞销库存,
            "命中子ASIN近30天日均销量": self.命中子ASIN近30天日均销量,
            "父ASIN汇总单月超龄仓租费用": self.父ASIN汇总单月超龄仓租费用,
        }


# -----------------------------------------------------------------------------
# 内部 helpers
# -----------------------------------------------------------------------------
def _safe_div(a: float | None, b: float | None) -> float | None:
    """安全除法，任一为 None 或除数为 0 → None。"""
    if a is None or b is None or b == 0:
        return None
    return a / b


def _算滚动均值(rows: list[dict], 字段: str, 天数: int) -> float | None:
    """取最近 N 行（按 stat_date DESC 已排好）的指定字段均值。
    有效值门槛分档：近3天要求 ≥2，近7天要求 ≥4，近30天要求 ≥15，其它按 天数×2/3 取整。
    不达门槛返回 None（业务侧应视为数据不足，走观察类，绝不当均值使用）。"""
    最低门槛 = {3: 2, 7: 4, 30: 15}.get(天数, max(2, 天数 * 2 // 3))
    vals = []
    for r in rows[:天数]:
        v = r.get(字段)
        if v is not None:
            vals.append(v)
    if len(vals) < 最低门槛:
        return None
    return sum(vals) / len(vals)


def _算滚动求和(rows: list[dict], 字段: str, 天数: int) -> float | None:
    """取最近 N 行的指定字段求和。全 None → None。"""
    total = 0.0
    has_any = False
    for r in rows[:天数]:
        v = r.get(字段)
        if v is not None:
            total += v
            has_any = True
    return total if has_any else None


def _算滚动均值_自然占比(rows: list[dict], 天数: int) -> tuple[float | None, float | None]:
    """计算近N天自然订单占比均值 + 自然订单数均值。
    自然单量 = max(0, 全部单量 - 广告单量)，占比 = 自然单量 / 全部单量。
    返回 (平均自然占比, 平均自然订单数)。"""
    ratios = []
    nat_orders = []
    for r in rows[:天数]:
        总单 = r.get("全部单量")
        广单 = r.get("广告单量")
        if 总单 is None or 总单 == 0:
            continue
        自然 = max(0, (总单 or 0) - (广单 or 0))
        ratios.append(自然 / 总单)
        nat_orders.append(自然)
    if len(ratios) < 2:
        return None, None
    return sum(ratios) / len(ratios), sum(nat_orders) / len(nat_orders)


def _算连续天数(
    rows: list[dict],
    字段: str,
    基线: float | None,
    方向: str = "上升",  # "上升"=异常方向是变大(ACOS/花费), "下降"=异常方向是变小(销量)
) -> int:
    """从最近一天往回数，连续满足"偏离方向"的最大天数。
    方向='上升'：当日值 > 基线 才算连续
    方向='下降'：当日值 < 基线 才算连续
    基线为 None → 返回 0。"""
    if 基线 is None:
        return 0
    count = 0
    for r in rows:
        v = r.get(字段)
        if v is None:
            break  # 数据断了一天就停
        if 方向 == "上升" and v > 基线:
            count += 1
        elif 方向 == "下降" and v < 基线:
            count += 1
        else:
            break
    return count


def _算月进度() -> float:
    """计算当月时间进度 (0-1)。
    月累计完成率需要从 sales_child.本月完成 算，在 聚合单产品() 中一并计算。"""
    today = dt.date.today()
    # 当月第一天
    month_start = today.replace(day=1)
    # 当月最后一天
    if today.month == 12:
        month_end = today.replace(year=today.year + 1, month=1, day=1) - dt.timedelta(days=1)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - dt.timedelta(days=1)
    total_days = month_end.day
    elapsed = (today - month_start).days + 1  # 含今天
    return elapsed / total_days if total_days > 0 else 0.0


def _算自然占比逐日(rows: list[dict], 天数: int = 7) -> list[float]:
    """取最近 N 天的逐日自然占比列表（从最早到最新）。
    任一天全部单量为空/0 → 返回空列表（不参与 S0 升级判定，避免"0 = 低于基线"污染）。"""
    if len(rows) < 天数:
        return []
    ratios = []
    for r in reversed(rows[:天数]):  # reversed → 从早到晚
        总单 = r.get("全部单量")
        广单 = r.get("广告单量") or 0
        if not 总单 or 总单 <= 0:
            return []
        自然 = max(0, 总单 - 广单)
        ratios.append(自然 / 总单)
    return ratios


def _算逐日值(rows: list[dict], 字段: str, 天数: int = 7) -> list[float]:
    """取最近 N 天的逐日字段值（从最早到最新）。
    数据完整性：任一天缺失或数据行数不足 N → 返回空列表 []（严禁填 0，会污染 S0 升级判定）。
    R3 各专项的"近7天逐日" S0 升级条件在拿到空列表时自然不满足，符合"没数据就说没数据"约定。"""
    # 行数不足
    if len(rows) < 天数:
        return []
    vals = []
    for r in reversed(rows[:天数]):
        v = r.get(字段)
        if v is None:
            return []           # 任一天缺失 → 整段作废，不参与 S0 升级
        vals.append(float(v))
    return vals


# -----------------------------------------------------------------------------
# 聚合单产品
# -----------------------------------------------------------------------------
def 聚合单产品(
    父ASIN: str,
    店铺账号: str,
    站点: str = "",
    销售数据: list[dict] | None = None,
    快照数据: dict | None = None,
) -> 每日聚合结果:
    """对单个父ASIN做全量聚合。
    可传入预加载的销售数据/快照数据（批量场景复用），不传则自行查询。

    Args:
        父ASIN: 父 ASIN
        店铺账号: 店铺账号名
        站点: 站点代码
        销售数据: 可选，预加载的 parent_daily_sales（30天）
        快照数据: 可选，预加载的 product_snapshots（含 listing/stock/tags）

    Returns:
        每日聚合结果 — 可直接解包传给 severity_grader 各专项函数。
    """
    缺失: list[str] = []

    # ---- 加载数据 ----
    if 销售数据 is None:
        销售数据 = store.query_parent_daily_sales(父ASIN, days=30)
    if 快照数据 is None:
        快照数据 = store.query_product_snapshots(父ASIN, 店铺账号) or {}

    listing = (快照数据 or {}).get("listing")
    stock = (快照数据 or {}).get("stock")
    tags = (快照数据 or {}).get("tags")

    # 子体目标数据
    子体列表 = store.query_sales_children(父ASIN, 店铺账号)

    # ---- 元信息 ----
    数据天数 = len(销售数据)
    if 数据天数 == 0:
        # 完全无数据 → 返回空结果
        return 每日聚合结果(
            父ASIN=父ASIN, 店铺账号=店铺账号, 站点=站点,
            缺失字段=["无任何销售数据"],
        )

    日期列表 = [r["stat_date"] for r in 销售数据]
    窗口起 = 日期列表[-1] if 日期列表 else None  # 最早
    窗口止 = 日期列表[0] if 日期列表 else None   # 最晚

    # ---- 销量指标 ----
    近3天日均销量 = _算滚动均值(销售数据, "全部单量", 3)
    过去7天日均销量 = _算滚动均值(销售数据, "全部单量", 7)
    近30天日均销量 = _算滚动均值(销售数据, "全部单量", 30)
    近7天逐日单量 = _算逐日值(销售数据, "全部单量", 7)
    销量连续下降天数 = _算连续天数(销售数据, "全部单量", 过去7天日均销量, 方向="下降")

    # ---- ACOS 指标 ----
    近7天平均ACOS = _算滚动均值(销售数据, "ACOS", 7)
    近3天平均ACOS = _算滚动均值(销售数据, "ACOS", 3)
    近3天累计广告花费 = _算滚动求和(销售数据, "广告花费", 3)
    近7天日均花费 = _算滚动均值(销售数据, "广告花费", 7)
    # 近3天累计点击：暂无点击数据
    近3天累计点击 = None
    ACOS连续超标天数 = _算连续天数(销售数据, "ACOS", 近7天平均ACOS, 方向="上升")

    # ---- 广告花费指标 ----
    近7天平均花费 = 近7天日均花费  # 语义等价（近7天日均花费也是滚动均值）
    近3天平均花费 = _算滚动均值(销售数据, "广告花费", 3)
    # 花费连续偏离：自动判断方向
    if 近7天平均花费 is not None and 近3天平均花费 is not None:
        if 近3天平均花费 >= 近7天平均花费:
            花费连续偏离天数 = _算连续天数(销售数据, "广告花费", 近7天平均花费, 方向="上升")
        else:
            花费连续偏离天数 = _算连续天数(销售数据, "广告花费", 近7天平均花费, 方向="下降")
    else:
        花费连续偏离天数 = 0

    # ---- 自然流量指标（从 全部单量 - 广告单量 计算）----
    近3天平均自然占比, 近3天自然订单数 = _算滚动均值_自然占比(销售数据, 3)
    近7天平均自然占比, 近7天自然订单数 = _算滚动均值_自然占比(销售数据, 7)
    近7天逐日自然占比 = _算自然占比逐日(销售数据, 7)

    # 前7天自然占比（再往前 7 天，即第 8~14 天）
    if len(销售数据) >= 14:
        前7天数据 = 销售数据[7:14]
        前7天平均自然占比, _ = _算滚动均值_自然占比(前7天数据, 7)
    else:
        前7天平均自然占比 = None

    # ---- 卡位指标（数据缺口）----
    近3天平均卡位 = None
    近7天平均卡位 = None
    近7天逐日卡位 = []
    缺失.append("近3天平均卡位")
    缺失.append("近7天平均卡位")
    缺失.append("近7天逐日卡位")

    # ---- 转化率（从快照读）----
    if listing:
        本周转化率 = listing.get("链接转化率")
        类目平均转化率 = listing.get("类目转化率")
    else:
        本周转化率 = None
        类目平均转化率 = None
    上周转化率 = None  # 无上周快照
    本周会话 = None     # 数据缺口
    上周会话 = None
    本周订单商品总数 = None
    上周订单商品总数 = None
    if 本周会话 is None:
        缺失.append("本周会话")
    if 上周转化率 is None:
        缺失.append("上周转化率")

    # ---- 评分 ----
    # 数据边界：本地库只有快照星级，无逐日评分历史 → 近7天平均评分 = None（严禁拿快照当均值）
    if listing:
        当前评分 = listing.get("星级")
    else:
        当前评分 = None
    近7天平均评分 = None       # 无逐日历史，缺失
    if 近7天平均评分 is None:
        缺失.append("近7天平均评分")
    if tags:
        目标评分 = tags.get("TARGET_STAR_RATE")
    else:
        目标评分 = None
    if 目标评分 is None:
        缺失.append("目标评分")

    # ---- 退款 ----
    # 数据边界：本地库 16 周退款率是快照，无严格"本周/上周"周维度数据 → 上周退款率 = None
    # 32 周退款率不能当"上周"用（时间尺度完全不同，会污染 R3 环比判定）
    if listing:
        本周退款率 = listing.get("16周退款率")
        上周退款率 = None      # 无严格上周数据，缺失
        类目平均退换货率 = listing.get("类目退换货率")
    else:
        本周退款率 = None
        上周退款率 = None
        类目平均退换货率 = None
    if 上周退款率 is None:
        缺失.append("上周退款率")

    # ---- 目标偏离 ----
    近3天日均单量 = 近3天日均销量  # 同一值
    月时间进度 = _算月进度()

    # 日均目标单量 + 月累计完成率：从子体汇总
    日均目标单量 = None
    月累计完成率 = None
    if 子体列表:
        总目标 = 0.0
        总完成 = 0.0
        for sc in 子体列表:
            t = sc.get("当月目标销量")
            c = sc.get("本月完成")
            if t is not None:
                总目标 += t
            if c is not None:
                总完成 += c
        if 总目标 > 0:
            # 日均目标 = 当月目标 / 当月天数
            today = dt.date.today()
            if today.month == 12:
                month_end = today.replace(year=today.year + 1, month=1, day=1) - dt.timedelta(days=1)
            else:
                month_end = today.replace(month=today.month + 1, day=1) - dt.timedelta(days=1)
            日均目标单量 = 总目标 / month_end.day
            # 月累计完成率 = 已完成 / 总目标
            月累计完成率 = 总完成 / 总目标 if 总目标 > 0 else None
    if 日均目标单量 is None:
        缺失.append("日均目标单量")
    if 月累计完成率 is None:
        缺失.append("月累计完成率")

    # ---- 放量未执行 ----
    目标ACOS = None
    if tags:
        # 目标ACOS 可能从 ERP 标签获取，暂无明确字段 → 尝试从 data 读取
        tags_data = tags.get("data")
        if isinstance(tags_data, str):
            import json
            tags_data = json.loads(tags_data)
        if isinstance(tags_data, dict):
            目标ACOS = tags_data.get("TARGET_ACOS") or tags_data.get("目标ACOS")
    if 目标ACOS is None:
        缺失.append("目标ACOS")

    # ---- 库存积压 ----
    if stock:
        可售库存 = stock.get("FBA可售库存")
    else:
        可售库存 = None

    # 未来N月目标累计_覆盖月数
    未来N月目标累计_覆盖月数 = None
    if 子体列表 and 可售库存 is not None and 可售库存 > 0:
        总未来目标 = 0.0
        未来月数 = 0
        for sc in 子体列表:
            for key in ["后1月目标销量", "后2月目标销量", "后3月目标销量"]:
                v = sc.get(key)
                if v is not None and v > 0:
                    总未来目标 += v
                    未来月数 += 1
        if 总未来目标 > 0 and 未来月数 > 0:
            月均目标 = 总未来目标 / 未来月数
            if 月均目标 > 0:
                未来N月目标累计_覆盖月数 = 可售库存 / 月均目标

    # 新品观察期
    是否新品观察期 = False
    有效销售天数_算 = sum(1 for r in 销售数据 if (r.get("全部单量") or 0) > 0)
    if 有效销售天数_算 < 7:
        是否新品观察期 = True

    # ---- 滞销（数据缺口）----
    # 超龄仓租/滞销库存数据暂无

    # ---- 汇总缺失字段 ----
    # 只保留真正为 None 且在计算中无法获取的
    关键字段检查 = {
        "近3天日均销量": 近3天日均销量,
        "过去7天日均销量": 过去7天日均销量,
        "近7天平均ACOS": 近7天平均ACOS,
        "近3天平均ACOS": 近3天平均ACOS,
        "近7天平均花费": 近7天平均花费,
        "近3天平均花费": 近3天平均花费,
    }
    for k, v in 关键字段检查.items():
        if v is None and k not in 缺失:
            缺失.append(k)

    return 每日聚合结果(
        父ASIN=父ASIN,
        店铺账号=店铺账号,
        站点=站点,
        # 销量
        近3天日均销量=近3天日均销量,
        过去7天日均销量=过去7天日均销量,
        近30天日均销量=近30天日均销量,
        近7天逐日单量=近7天逐日单量,
        销量连续下降天数=销量连续下降天数,
        # ACOS
        近7天平均ACOS=近7天平均ACOS,
        近3天平均ACOS=近3天平均ACOS,
        近3天累计广告花费=近3天累计广告花费,
        近7天日均花费=近7天日均花费,
        近3天累计点击=近3天累计点击,
        ACOS连续超标天数=ACOS连续超标天数,
        # 广告花费
        近7天平均花费=近7天平均花费,
        近3天平均花费=近3天平均花费,
        花费连续偏离天数=花费连续偏离天数,
        # 目标偏离
        近3天日均单量=近3天日均单量,
        日均目标单量=日均目标单量,
        月累计完成率=月累计完成率,
        月时间进度=月时间进度,
        # 自然流量
        近3天平均自然占比=近3天平均自然占比,
        近7天平均自然占比=近7天平均自然占比,
        前7天平均自然占比=前7天平均自然占比,
        近3天自然订单数=近3天自然订单数,
        近7天自然订单数=近7天自然订单数,
        近7天逐日自然占比=近7天逐日自然占比,
        # 卡位
        近3天平均卡位=近3天平均卡位,
        近7天平均卡位=近7天平均卡位,
        近7天逐日卡位=近7天逐日卡位,
        # 转化率
        本周转化率=本周转化率,
        上周转化率=上周转化率,
        类目平均转化率=类目平均转化率,
        本周会话=本周会话,
        上周会话=上周会话,
        本周订单商品总数=本周订单商品总数,
        上周订单商品总数=上周订单商品总数,
        # 评分
        当前评分=当前评分,
        近7天平均评分=近7天平均评分,
        目标评分=目标评分,
        # 退款
        本周退款率=本周退款率,
        上周退款率=上周退款率,
        类目平均退换货率=类目平均退换货率,
        # 放量未执行
        目标ACOS=目标ACOS,
        # 库存
        可售库存=可售库存,
        未来N月目标累计_覆盖月数=未来N月目标累计_覆盖月数,
        是否新品观察期=是否新品观察期,
        有效销售天数=有效销售天数_算,
        # 滞销
        有滞销库存=False,
        命中子ASIN滞销库存=None,
        命中子ASIN近30天日均销量=None,
        父ASIN汇总单月超龄仓租费用=None,
        # 元信息
        数据窗口_天数=数据天数,
        数据窗口_起=窗口起,
        数据窗口_止=窗口止,
        缺失字段=缺失,
    )


# -----------------------------------------------------------------------------
# 批量聚合
# -----------------------------------------------------------------------------
def 批量聚合(
    产品列表: list[dict] | None = None,
    店铺筛选: str | None = None,
) -> dict[str, 每日聚合结果]:
    """批量聚合所有（或指定）产品的每日指标。

    Args:
        产品列表: 可选，指定产品列表 [{"parent_asin":..., "shop_account":..., "site_code":...}]
                  不传则自动从 daily_product_sales 去重获取。
        店铺筛选: 可选，只聚合指定店铺。

    Returns:
        {父ASIN: 每日聚合结果} 字典。
    """
    if 产品列表 is None:
        产品列表 = store.query_active_products()

    if 店铺筛选:
        产品列表 = [p for p in 产品列表 if p["shop_account"] == 店铺筛选]

    结果: dict[str, 每日聚合结果] = {}
    log.info("开始批量聚合 %d 个产品...", len(产品列表))

    for i, p in enumerate(产品列表):
        parent = p["parent_asin"]
        shop = p["shop_account"]
        site = p.get("site_code", "") or ""

        try:
            # 预加载数据：查询一次
            销售数据 = store.query_parent_daily_sales(parent, days=30)
            快照数据 = store.query_product_snapshots(parent, shop) or {}

            r = 聚合单产品(
                父ASIN=parent,
                店铺账号=shop,
                站点=site,
                销售数据=销售数据,
                快照数据=快照数据,
            )
            结果[parent] = r
        except Exception as e:
            log.warning("产品 %s (%s) 聚合失败: %s", parent, shop, e)
            # 返回一个空结果标记失败
            结果[parent] = 每日聚合结果(
                父ASIN=parent, 店铺账号=shop, 站点=site,
                缺失字段=[f"聚合异常: {e}"],
            )

        if (i + 1) % 20 == 0:
            log.info("  进度: %d/%d", i + 1, len(产品列表))

    log.info("批量聚合完成: %d 个产品", len(结果))
    return 结果


# -----------------------------------------------------------------------------
# 便捷：导出为 severity_grader 可直接 **kwargs 的 dict
# -----------------------------------------------------------------------------
def 导出为判参(结果: 每日聚合结果) -> dict[str, Any]:
    """将 每日聚合结果 转为 dict，可直接 **dict 传给 severity_grader 专项函数。
    注意：只会包含非 None 的字段；severity_grader 各函数用不到的字段会被忽略。"""
    from dataclasses import fields
    d = {}
    for f in fields(结果):
        v = getattr(结果, f.name)
        if v is not None:
            d[f.name] = v
    return d


# -----------------------------------------------------------------------------
# 自检
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    store.init_db()

    print("=" * 70)
    print("daily_monitor 自检")
    print("=" * 70)

    # 批量聚合
    全部 = 批量聚合()
    print(f"\n聚合完成: {len(全部)} 个产品")

    # 统计
    有缺失 = sum(1 for r in 全部.values() if r.缺失字段)
    有ACOS = sum(1 for r in 全部.values() if r.近7天平均ACOS is not None)
    有自然占比 = sum(1 for r in 全部.values() if r.近7天平均自然占比 is not None)
    有目标 = sum(1 for r in 全部.values() if r.日均目标单量 is not None)
    有库存 = sum(1 for r in 全部.values() if r.可售库存 is not None)
    print(f"  有缺失字段的产品: {有缺失}")
    print(f"  有ACOS数据: {有ACOS}")
    print(f"  有自然占比数据: {有自然占比}")
    print(f"  有目标数据: {有目标}")
    print(f"  有库存数据: {有库存}")

    # 抽查前 3 个产品
    print("\n" + "-" * 70)
    print("抽查 3 个产品:")
    print("-" * 70)
    for i, (parent, r) in enumerate(list(全部.items())[:3]):
        print(f"\n--- 产品 {i+1}: {parent} ({r.店铺账号}) ---")
        print(f"  数据窗口: {r.数据窗口_起} ~ {r.数据窗口_止} ({r.数据窗口_天数}天)")
        print(f"  有效销售天数: {r.有效销售天数}")
        print(f"  近3天日均销量: {r.近3天日均销量}")
        print(f"  过去7天日均销量: {r.过去7天日均销量}")
        print(f"  销量连续下降天数: {r.销量连续下降天数}")
        print(f"  近7天平均ACOS: {r.近7天平均ACOS}")
        print(f"  近3天平均ACOS: {r.近3天平均ACOS}")
        print(f"  ACOS连续超标天数: {r.ACOS连续超标天数}")
        print(f"  近7天平均花费: {r.近7天平均花费}")
        print(f"  近7天平均自然占比: {r.近7天平均自然占比}")
        print(f"  日均目标单量: {r.日均目标单量}")
        print(f"  月累计完成率: {r.月累计完成率}")
        print(f"  可售库存: {r.可售库存}")
        print(f"  未来N月目标累计_覆盖月数: {r.未来N月目标累计_覆盖月数}")
        print(f"  当前评分: {r.当前评分}")
        print(f"  本周退款率: {r.本周退款率}")
        print(f"  缺失字段 ({len(r.缺失字段)}): {r.缺失字段[:10]}{'...' if len(r.缺失字段)>10 else ''}")
