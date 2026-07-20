"""模块 2 异常识别器

【业务边界】
输入：产品的状态字段/数据（来自 MCP 与本地库）
输出：命中的异常点位清单（含 作用层级、命中变体、类型、是否跳过表现型）
     + 数据不足场景 → 观察类结果

【规则来源】docs/业务巡检Agent-代码实现版.md 模块 2
【参数来源】inspector/rules/R2_异常字典.yaml

【设计约定】
1. 一个点位一个 detect_XXX 函数（高内聚），互不 import
2. 现象即原因型：本文件里检测命中条件
3. 表现型：本文件不检测命中，由 daily_monitor 触发 R3 判定
4. 数据不足 → 明确返回观察类结果，绝不猜
5. 每个命中带 判定过程（人话）+ 触发字段（可回溯）

【与 R3 的分工】
- R2 detect_XXX 只回答"命中/未命中"
- R3 severity_grader 回答"S 是多少"
- 现象即原因型点位的默认 S 从 R3.默认严重度表 读取
- 表现型点位的 S 由 R3 各专项计算
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

观察_数据不足 = "数据不足"


# -----------------------------------------------------------------------------
# 数据契约
# -----------------------------------------------------------------------------
@dataclass
class 命中异常:
    """单个命中的异常记录。"""
    问题点位: str
    作用层级: str                    # 链接级 | 变体级
    类型: str                        # 现象即原因型 | 表现型
    命中变体: str | None = None      # 具体子体，如 "黑色/M"
    变体重要性: str | None = None    # 主要色 / 次要色 / 长尾色
    命中依据: str = ""               # 一句人话
    触发字段: dict = field(default_factory=dict)  # 触发的字段值，供回溯

    # 与 R3/R4 对接
    默认严重度: str | None = None    # 现象即原因型才有；表现型为 None（走专项）

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class 观察类结果:
    问题点位: str
    观察类型: str = 观察_数据不足
    原因: str = ""
    缺失字段: list[str] = field(default_factory=list)
    上下文: dict = field(default_factory=dict)


# -----------------------------------------------------------------------------
# 参数加载
# -----------------------------------------------------------------------------
_R2_PATH = Path(__file__).resolve().parent.parent / "rules" / "R2_异常字典.yaml"
_R3_PATH = Path(__file__).resolve().parent.parent / "rules" / "R3_严重度阈值.yaml"

_R2缓存: dict | None = None
_R3缓存: dict | None = None


def 加载R2(force_reload: bool = False) -> dict:
    global _R2缓存
    if _R2缓存 is None or force_reload:
        with open(_R2_PATH, encoding="utf-8") as f:
            _R2缓存 = yaml.safe_load(f)
        log.info("R2 异常字典已加载 版本=%s", _R2缓存.get("版本"))
    return _R2缓存


def 加载R3(force_reload: bool = False) -> dict:
    """R2 用 R3 的 默认严重度表 补充"默认严重度"。"""
    global _R3缓存
    if _R3缓存 is None or force_reload:
        with open(_R3_PATH, encoding="utf-8") as f:
            _R3缓存 = yaml.safe_load(f)
    return _R3缓存


# -----------------------------------------------------------------------------
# 元数据查询：作用层级、类型、默认严重度
# -----------------------------------------------------------------------------
def 查作用层级(点位: str, r2: dict | None = None) -> str | None:
    """按 §2.0 归类返回 '链接级' / '变体级' / '按命中对象'。"""
    if r2 is None:
        r2 = 加载R2()
    归 = r2["作用层级归类"]
    if 点位 in 归["变体级"]:
        return "变体级"
    if 点位 in 归["链接级"]:
        return "链接级"
    if 点位 in 归["按命中对象"]:
        return "按命中对象"
    return None


def 查大类(点位: str, r2: dict | None = None) -> tuple[str | None, str | None]:
    """返回 (大类编号, 类型)，如 ('2.4', '现象即原因型')。"""
    if r2 is None:
        r2 = 加载R2()
    for 编号, 元 in r2["异常大类"].items():
        # 大类下具体点位由文档区分（本文件按前缀匹配简化）
        pass
    # 用 类型 字段查
    表现型点位 = r2["表现型跳过规则"]["自身即表现型"]
    if 点位 in 表现型点位:
        return None, "表现型"
    if 点位 == "放量未执行":
        return "2.5", "现象即原因型"  # 例外
    return None, "现象即原因型"


def 查默认严重度(点位: str, r3: dict | None = None) -> str | None:
    """从 R3 默认严重度表读默认 S。表现型/专项类返回 None。"""
    if r3 is None:
        r3 = 加载R3()
    v = r3["默认严重度表"].get(点位)
    if v in ("S0", "S1", "S2"):
        return v
    return None


# -----------------------------------------------------------------------------
# §2.0.1 表现型异常跳过规则
# -----------------------------------------------------------------------------
def 是否触发跳过表现型(点位: str, 命中变体重要性: str | None, 是否全变体命中: bool = False,
                       r2: dict | None = None) -> tuple[bool, str]:
    """按 §2.0.1 判断是否需要跳过父ASIN的表现型异常判定与查因。

    返回 (是否跳过, 判定理由)
    """
    if r2 is None:
        r2 = 加载R2()
    规则 = r2["表现型跳过规则"]

    # 无条件跳过
    if 点位 in 规则["无条件跳过"]:
        return True, f"「{点位}」属无条件跳过清单"

    # 不跳过清单
    if 点位 in 规则["不跳过"]:
        return False, f"「{点位}」属不跳过清单"

    # 表现型自身不参与
    if 点位 in 规则["自身即表现型"]:
        return False, f"「{点位}」为表现型点位，不参与本规则"

    # 分层跳过
    if 点位 in 规则["分层跳过"]["点位"]:
        if 是否全变体命中:
            return True, f"「{点位}」整条Listing/全变体命中，视同主要色，跳过"
        if 命中变体重要性 == "主要色":
            return True, f"「{点位}」命中主要色，跳过父ASIN表现型"
        if 命中变体重要性 in ("次要色", "长尾色"):
            return False, f"「{点位}」命中{命中变体重要性}，不跳过父ASIN；该变体单独处理"
        # 变体重要性 None 但需要分层 → 数据不足，保守不跳过
        return False, f"「{点位}」在分层跳过清单但缺变体重要性数据，保守不跳过"

    return False, f"「{点位}」不在任何跳过规则清单"


# -----------------------------------------------------------------------------
# §2.4 库存类检测（现象即原因型，条件明确）
# -----------------------------------------------------------------------------
def detect_FBA可售库存为0(
    FBA可售库存: float | None = None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r2: dict | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.4 FBA可售库存为0。命中返回 命中异常；未命中返回 None；数据缺失返回 观察。"""
    if FBA可售库存 is None:
        return 观察类结果(问题点位="FBA可售库存为0", 原因="缺失 FBA可售库存",
                     缺失字段=["FBA可售库存"])
    if FBA可售库存 == 0:
        r3 = r3 or 加载R3()
        return 命中异常(
            问题点位="FBA可售库存为0",
            作用层级="变体级",
            类型="现象即原因型",
            命中变体=子体ASIN,
            变体重要性=变体重要性,
            命中依据=f"FBA可售库存 = 0（子体 {子体ASIN or '未指定'}）",
            触发字段={"FBA可售库存": 0},
            默认严重度=查默认严重度("FBA可售库存为0", r3),
        )
    return None


def detect_库存不足(
    FBA可售库存: float | None = None,
    过去7天日均销量: float | None = None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r2: dict | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.4 库存不足：FBA可售库存 > 0 但 < 14天预计消耗。"""
    缺 = [k for k, v in {"FBA可售库存": FBA可售库存, "过去7天日均销量": 过去7天日均销量}.items() if v is None]
    if 缺:
        return 观察类结果(问题点位="库存不足", 原因=f"缺失字段 {缺}", 缺失字段=缺)

    if FBA可售库存 == 0:
        return None  # 归 FBA可售库存为0

    r2 = r2 or 加载R2()
    预计消耗天数 = r2["库存不足"]["预计消耗天数"]
    预计消耗 = 过去7天日均销量 * 预计消耗天数
    if FBA可售库存 < 预计消耗:
        r3 = r3 or 加载R3()
        return 命中异常(
            问题点位="库存不足",
            作用层级="变体级",
            类型="现象即原因型",
            命中变体=子体ASIN,
            变体重要性=变体重要性,
            命中依据=f"FBA可售库存 {FBA可售库存} < 未来{预计消耗天数}天预计消耗 {预计消耗:.1f}（日均 {过去7天日均销量}）",
            触发字段={"FBA可售库存": FBA可售库存, "预计消耗": 预计消耗},
            默认严重度=查默认严重度("库存不足", r3),
        )
    return None


# -----------------------------------------------------------------------------
# §2.2 内容完整性（简化：只判缺失/审核/前台异常，不做视觉）
# -----------------------------------------------------------------------------
def detect_主图异常(
    主图字段: str | None,
    审核状态: str | None,
    前台展示: str | None,
    ERP图片基准: str | None = None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r2: dict | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.2 主图异常。第一版不做视觉判断。"""
    if 主图字段 is None and 审核状态 is None and 前台展示 is None:
        return 观察类结果(问题点位="主图异常", 原因="所有主图相关字段均缺失",
                     缺失字段=["主图字段", "审核状态", "前台展示"])

    异常原因 = []
    触发 = {}
    if 主图字段 in (None, "", "缺失"):
        异常原因.append("主图缺失")
        触发["主图字段"] = 主图字段
    if 审核状态 in ("未通过", "审核中", "异常"):
        异常原因.append(f"审核状态={审核状态}")
        触发["审核状态"] = 审核状态
    if 前台展示 == "异常":
        异常原因.append("前台展示异常")
        触发["前台展示"] = 前台展示
    if ERP图片基准 is not None and 主图字段 is not None and 主图字段 != ERP图片基准:
        异常原因.append("主图与 ERP 基准不一致（疑似被改）")
        触发["ERP基准差异"] = True

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="主图异常",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=子体ASIN,
        变体重要性=变体重要性,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("主图异常", r3),
    )


def detect_图片异常(
    副图数量: int | None,
    审核状态: str | None,
    前台展示: str | None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r2: dict | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.2 图片异常（副图数量/审核/前台）。"""
    if 副图数量 is None and 审核状态 is None and 前台展示 is None:
        return 观察类结果(问题点位="图片异常", 原因="所有图片相关字段均缺失",
                     缺失字段=["副图数量", "审核状态", "前台展示"])

    r2 = r2 or 加载R2()
    最低 = r2["图片异常"]["最低副图数量"]
    异常原因 = []
    触发 = {}
    if 副图数量 is not None and 副图数量 < 最低:
        异常原因.append(f"副图数量 {副图数量} < {最低}")
        触发["副图数量"] = 副图数量
    if 审核状态 in ("未通过", "异常"):
        异常原因.append(f"审核状态={审核状态}")
        触发["审核状态"] = 审核状态
    if 前台展示 == "异常":
        异常原因.append("前台展示异常")
        触发["前台展示"] = 前台展示

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="图片异常",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=子体ASIN,
        变体重要性=变体重要性,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("图片异常", r3),
    )


def detect_标题异常(
    标题字段: str | None,
    审核状态: str | None,
    ERP标题基准: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.2 标题异常（链接级）。"""
    if 标题字段 is None and 审核状态 is None:
        return 观察类结果(问题点位="标题异常", 原因="所有标题相关字段均缺失",
                     缺失字段=["标题字段", "审核状态"])

    异常原因 = []
    触发 = {}
    if 标题字段 in (None, "", "缺失"):
        异常原因.append("标题缺失")
        触发["标题字段"] = 标题字段
    if 审核状态 == "未通过":
        异常原因.append(f"审核状态={审核状态}")
        触发["审核状态"] = 审核状态
    if ERP标题基准 is not None and 标题字段 is not None and 标题字段 != ERP标题基准:
        异常原因.append("标题与 ERP 基准不一致（疑似被改）")
        触发["ERP基准差异"] = True

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="标题异常",
        作用层级="链接级",
        类型="现象即原因型",
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("标题异常", r3),
    )


def detect_五点异常(
    五点内容项数: int | None,
    审核状态: str | None,
    r2: dict | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.2 五点异常（链接级）。"""
    if 五点内容项数 is None and 审核状态 is None:
        return 观察类结果(问题点位="五点异常", 原因="所有五点相关字段均缺失",
                     缺失字段=["五点内容项数", "审核状态"])

    r2 = r2 or 加载R2()
    最低 = r2["五点异常"]["最低五点数量"]
    异常原因 = []
    触发 = {}
    if 五点内容项数 is not None and 五点内容项数 < 最低:
        异常原因.append(f"五点项数 {五点内容项数} < {最低}")
        触发["五点内容项数"] = 五点内容项数
    if 审核状态 in ("未通过", "异常"):
        异常原因.append(f"审核状态={审核状态}")
        触发["审核状态"] = 审核状态

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="五点异常",
        作用层级="链接级",
        类型="现象即原因型",
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("五点异常", r3),
    )


def detect_A加异常(
    A加内容状态: str | None,
    审核状态: str | None,
    前台展示: str | None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.2 A+ 异常（变体级）。函数名不能有 '+'，用 '加' 代替；输出仍为 'A+异常'。"""
    if A加内容状态 is None and 审核状态 is None and 前台展示 is None:
        return 观察类结果(问题点位="A+异常", 原因="所有 A+ 相关字段均缺失",
                     缺失字段=["A+内容状态", "审核状态", "前台展示"])

    异常原因 = []
    触发 = {}
    if A加内容状态 in ("未创建", "缺失"):
        异常原因.append(f"A+内容={A加内容状态}")
        触发["A+内容状态"] = A加内容状态
    if 审核状态 in ("未通过", "审核中"):
        异常原因.append(f"审核状态={审核状态}")
        触发["审核状态"] = 审核状态
    if 前台展示 == "未展示":
        异常原因.append("前台未展示")
        触发["前台展示"] = 前台展示

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="A+异常",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=子体ASIN,
        变体重要性=变体重要性,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("A+异常", r3),
    )


# -----------------------------------------------------------------------------
# §2.3 价格与促销
# -----------------------------------------------------------------------------
def detect_变体价差异常(
    基准价: float | None,
    对比子体: dict | None,     # {变体重要性, 分类:'尺码'|'颜色', 到手价, ASIN?}
    运营动作导致: bool = False,
    r2: dict | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.3 变体价差异常。分尺码分支 / 颜色分支。"""
    if 基准价 is None or 对比子体 is None or 对比子体.get("到手价") is None:
        return 观察类结果(问题点位="变体价差异常", 原因="基准价或对比子体到手价缺失",
                     缺失字段=[k for k in ("基准价", "对比子体.到手价") if k not in locals()])

    if 基准价 == 0:
        return 观察类结果(问题点位="变体价差异常", 原因="基准价为0，无法计算差距比例")

    r2 = r2 or 加载R2()
    到手价 = 对比子体["到手价"]
    # 分位对齐后再比较，避免 $16.99 - $11.99 = 4.9999... 无法 >= 5 的浮点精度误差
    差距 = round(到手价 - 基准价, 2)
    绝对差距 = round(abs(差距), 2)
    差距比例 = round(绝对差距 / 基准价, 4)
    分类 = 对比子体.get("分类")

    if 分类 == "尺码":
        cfg = r2["变体价差异常"]["尺码分支"]
    elif 分类 == "颜色":
        cfg = r2["变体价差异常"]["颜色分支"]
    else:
        return 观察类结果(问题点位="变体价差异常",
                     原因=f"未知分类 {分类}（应为 尺码/颜色）",
                     缺失字段=["对比子体.分类"])

    命中 = 差距比例 >= cfg["差距比例阈值"] and 绝对差距 >= cfg["绝对差距阈值_USD"]
    if not 命中:
        return None

    if 运营动作导致:
        # 标记'需运营复核'，不进正式打分（返回观察类结果代表非正式异常）
        return 观察类结果(
            问题点位="变体价差异常",
            观察类型="需运营复核",
            原因=f"{分类}分支命中价差（{差距比例:.1%}，${绝对差距:.2f}），但由运营动作导致",
            上下文={"基准价": 基准价, "对比价": 到手价, "分类": 分类},
        )

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="变体价差异常",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=对比子体.get("ASIN"),
        变体重要性=对比子体.get("变体重要性"),
        命中依据=f"{分类}分支：对比价 ${到手价} vs 基准价 ${基准价}，差 {差距比例:.1%} ${绝对差距:.2f}（阈值 {cfg['差距比例阈值']:.0%}/${cfg['绝对差距阈值_USD']}）",
        触发字段={"基准价": 基准价, "对比价": 到手价, "差距比例": round(差距比例, 4)},
        默认严重度=查默认严重度("变体价差异常", r3),
    )


def detect_促销异常(
    ERP活动配置存在: bool | None,
    前台促销展示: str | None,
    活动审核状态: str | None,
    前台活动类型: str | None = None,
    ERP活动类型: str | None = None,
    前台售价: float | None = None,
    划线价: float | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.3 促销异常（链接级）。"""
    if (ERP活动配置存在 is None and 前台促销展示 is None and 活动审核状态 is None
            and 前台售价 is None and 划线价 is None):
        return 观察类结果(问题点位="促销异常", 原因="促销相关字段均缺失",
                     缺失字段=["ERP活动配置存在", "前台促销展示", "活动审核状态"])

    异常原因 = []
    触发 = {}
    if ERP活动配置存在 and 前台促销展示 == "未展示":
        异常原因.append("ERP有活动配置但前台未展示")
        触发["ERP配置"] = True
        触发["前台展示"] = 前台促销展示
    if 活动审核状态 in ("未通过", "异常"):
        异常原因.append(f"活动审核状态={活动审核状态}")
        触发["活动审核状态"] = 活动审核状态
    if 前台活动类型 and ERP活动类型 and 前台活动类型 != ERP活动类型:
        异常原因.append(f"前台活动类型({前台活动类型}) != ERP({ERP活动类型})")
        触发["活动类型不一致"] = True
    if 划线价 is not None and 前台售价 is not None and round(划线价, 2) == round(前台售价, 2):
        异常原因.append(f"划线价 ${划线价:.2f} 与前台售价相同，折扣展示未生效")
        触发["前台售价"] = 前台售价
        触发["划线价"] = 划线价

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="促销异常",
        作用层级="链接级",
        类型="现象即原因型",
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("促销异常", r3),
    )


# -----------------------------------------------------------------------------
# §2.1 链接状态（依赖 MCP 提供状态字段；命门数据）
# -----------------------------------------------------------------------------
def detect_链接不可售(
    后台可售状态: str | None = None,
    禁止显示搜索结果: bool | None = None,
    抑制标记: bool | None = None,
    下架标记: bool | None = None,
    报价状态: str | None = None,
    前台展示状态: str | None = None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.1 链接不可售。命门1 数据未完全接入前多数字段为 None。"""
    所有字段 = {
        "后台可售状态": 后台可售状态,
        "禁止显示搜索结果": 禁止显示搜索结果,
        "抑制标记": 抑制标记,
        "下架标记": 下架标记,
        "报价状态": 报价状态,
        "前台展示状态": 前台展示状态,
    }
    if all(v is None for v in 所有字段.values()):
        return 观察类结果(问题点位="链接不可售", 原因="所有状态字段均缺失（命门1数据源未接入）",
                     缺失字段=list(所有字段.keys()))

    异常原因 = []
    触发 = {}
    if 后台可售状态 and 后台可售状态 != "可售":
        异常原因.append(f"后台可售状态={后台可售状态}")
        触发["后台可售状态"] = 后台可售状态
    if 禁止显示搜索结果 is True:
        异常原因.append("禁止显示搜索结果=是")
        触发["禁止显示搜索结果"] = True
    if 抑制标记 is True:
        异常原因.append("抑制标记=是")
        触发["抑制标记"] = True
    if 下架标记 is True:
        异常原因.append("下架标记=是")
        触发["下架标记"] = True
    if 报价状态 in ("缺失", "异常"):
        异常原因.append(f"报价状态={报价状态}")
        触发["报价状态"] = 报价状态
    if 前台展示状态 == "不可展示":
        异常原因.append("前台不可展示")
        触发["前台展示状态"] = 前台展示状态

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="链接不可售",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=子体ASIN,
        变体重要性=变体重要性,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("链接不可售", r3),
    )


def detect_BuyBox丢失(
    BuyBox状态: str | None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.1 Buy Box 丢失。"""
    if BuyBox状态 is None:
        return 观察类结果(问题点位="Buy Box丢失", 原因="缺失 BuyBox 状态（命门1数据源未接入）",
                     缺失字段=["BuyBox状态"])
    if BuyBox状态 == "拥有":
        return None
    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="Buy Box丢失",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=子体ASIN,
        变体重要性=变体重要性,
        命中依据=f"Buy Box 状态={BuyBox状态}（非'拥有'）",
        触发字段={"BuyBox状态": BuyBox状态},
        默认严重度=查默认严重度("Buy Box丢失", r3),
    )


def detect_父子体关系异常(
    父体结构状态: str | None,
    全部子体挂接关系: dict | None = None,     # {子ASIN: 是否挂接}
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.1 父子体关系异常：父体被拆、全部子体掉出。"""
    if 父体结构状态 is None and 全部子体挂接关系 is None:
        return 观察类结果(问题点位="父子体关系异常", 原因="父体结构与挂接关系数据均缺失",
                     缺失字段=["父体结构状态", "全部子体挂接关系"])

    命中 = False
    原因 = []
    if 父体结构状态 == "被拆":
        命中 = True
        原因.append("父体结构=被拆")
    if 全部子体挂接关系 and len(全部子体挂接关系) > 0:
        全部脱离 = all(not v for v in 全部子体挂接关系.values())
        if 全部脱离:
            命中 = True
            原因.append("全部子体脱离父体挂接")

    if not 命中:
        return None
    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="父子体关系异常",
        作用层级="链接级",
        类型="现象即原因型",
        命中依据=" ; ".join(原因),
        触发字段={"父体结构状态": 父体结构状态},
        默认严重度=查默认严重度("父子体关系异常", r3),
    )


def detect_变体掉线(
    父体结构状态: str | None,
    子体挂接关系: dict | None = None,      # {子ASIN: 是否挂接}
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.1 变体掉线：父体在但部分子体脱离。"""
    if 子体挂接关系 is None:
        return 观察类结果(问题点位="变体掉线", 原因="缺失子体挂接关系",
                     缺失字段=["子体挂接关系"])

    if not 子体挂接关系:
        return None
    脱离数 = sum(1 for v in 子体挂接关系.values() if not v)
    总数 = len(子体挂接关系)

    if 脱离数 == 0:
        return None

    if 父体结构状态 == "被拆" or 脱离数 == 总数:
        # 全部脱离归 父子体关系异常
        return None

    r3 = r3 or 加载R3()
    脱离子体 = [k for k, v in 子体挂接关系.items() if not v]
    return 命中异常(
        问题点位="变体掉线",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=",".join(脱离子体[:3]),
        命中依据=f"{脱离数}/{总数} 个子体脱离父体（父体结构仍在）",
        触发字段={"脱离子体": 脱离子体},
        默认严重度=查默认严重度("变体掉线", r3),
    )


# -----------------------------------------------------------------------------
# §2.6 合规与属性
# -----------------------------------------------------------------------------
def detect_合规异常(
    平台合规状态: str | None,
    合规提示: str | None = None,
    认证要求未满足: bool | None = None,
    平台已限制: bool | None = None,
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.6 合规异常（固定子体级）。"""
    if 平台合规状态 is None and 合规提示 is None and 认证要求未满足 is None:
        return 观察类结果(问题点位="合规异常", 原因="合规相关字段均缺失",
                     缺失字段=["平台合规状态", "合规提示", "认证要求未满足"])

    异常原因 = []
    触发 = {}
    if 平台合规状态 == "违规":
        异常原因.append("平台合规状态=违规")
        触发["平台合规状态"] = 平台合规状态
    if 合规提示:
        异常原因.append(f"合规提示: {合规提示}")
        触发["合规提示"] = 合规提示
    if 认证要求未满足 and 平台已限制:
        异常原因.append("认证要求未满足且平台已限制")
        触发["认证受限"] = True

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="合规异常",
        作用层级="变体级",
        类型="现象即原因型",
        命中变体=子体ASIN,
        变体重要性=变体重要性,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("合规异常", r3),
    )


def detect_类目异常(
    类目路径: str | None,
    ERP类目基准: str | None,
    平台提示: str | None = None,
    命中层级: str = "链接级",    # 由调用方指定：命中具体子体传 '变体级'
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.6 类目异常。按命中对象决定层级。"""
    if 类目路径 is None and ERP类目基准 is None and 平台提示 is None:
        return 观察类结果(问题点位="类目异常", 原因="类目相关字段均缺失",
                     缺失字段=["类目路径", "ERP类目基准", "平台提示"])

    异常原因 = []
    触发 = {}
    if 类目路径 and ERP类目基准 and 类目路径 != ERP类目基准:
        异常原因.append(f"类目路径 != ERP基准")
        触发["类目路径"] = 类目路径
        触发["ERP类目基准"] = ERP类目基准
    if 平台提示 == "类目不匹配":
        异常原因.append("平台提示: 类目不匹配")
        触发["平台提示"] = 平台提示

    if not 异常原因:
        return None

    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="类目异常",
        作用层级=命中层级,
        类型="现象即原因型",
        命中变体=子体ASIN if 命中层级 == "变体级" else None,
        变体重要性=变体重要性 if 命中层级 == "变体级" else None,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("类目异常", r3),
    )


def detect_属性信息异常(
    属性字段: dict | None,
    ERP属性基准: dict | None,
    页面展示: dict | None = None,
    命中层级: str = "链接级",
    子体ASIN: str | None = None,
    变体重要性: str | None = None,
    r3: dict | None = None,
) -> 命中异常 | 观察类结果 | None:
    """§2.6 属性信息异常。"""
    if 属性字段 is None and ERP属性基准 is None:
        return 观察类结果(问题点位="属性信息异常", 原因="属性相关字段均缺失",
                     缺失字段=["属性字段", "ERP属性基准"])

    异常原因 = []
    触发 = {}
    if 属性字段 is not None:
        缺失属性 = [k for k, v in 属性字段.items() if v in (None, "", "缺失")]
        if 缺失属性:
            异常原因.append(f"属性缺失: {缺失属性}")
            触发["缺失属性"] = 缺失属性
    if 属性字段 and ERP属性基准:
        不一致 = [k for k in 属性字段 if k in ERP属性基准 and 属性字段[k] != ERP属性基准[k]]
        if 不一致:
            异常原因.append(f"属性 vs ERP基准不一致: {不一致}")
            触发["不一致属性"] = 不一致

    if not 异常原因:
        return None
    r3 = r3 or 加载R3()
    return 命中异常(
        问题点位="属性信息异常",
        作用层级=命中层级,
        类型="现象即原因型",
        命中变体=子体ASIN if 命中层级 == "变体级" else None,
        变体重要性=变体重要性 if 命中层级 == "变体级" else None,
        命中依据=" ; ".join(异常原因),
        触发字段=触发,
        默认严重度=查默认严重度("属性信息异常", r3),
    )


# -----------------------------------------------------------------------------
# 单元测试：跑 yaml 里的算例
# -----------------------------------------------------------------------------
def 跑内置算例() -> list[tuple[str, bool, Any]]:
    r2 = 加载R2(force_reload=True)
    r3 = 加载R3(force_reload=True)
    输出 = []
    for 算例 in r2.get("单元测试算例", []):
        名称 = 算例["场景"]
        # 跳过规则测试
        if 名称.startswith("跳过规则"):
            跳, 理由 = 是否触发跳过表现型(算例["命中点位"], 算例["命中变体重要性"], r2=r2)
            通过 = 跳 == 算例["预期跳过表现型"]
            输出.append((名称, 通过, f"实际={跳} 期望={算例['预期跳过表现型']} · {理由}"))
            continue

        点位 = 算例["点位"]
        输入 = 算例["输入"]
        期望命中 = 算例.get("预期命中")
        期望观察 = 算例.get("预期观察类")

        if 点位 == "库存不足":
            r = detect_库存不足(**输入, r2=r2, r3=r3)
        elif 点位 == "FBA可售库存为0":
            r = detect_FBA可售库存为0(**输入, r2=r2, r3=r3)
        elif 点位 == "变体价差异常":
            r = detect_变体价差异常(**输入, r2=r2, r3=r3)
        elif 点位 == "五点异常":
            r = detect_五点异常(**输入, r2=r2, r3=r3)
        elif 点位 == "图片异常":
            r = detect_图片异常(**输入, r2=r2, r3=r3)
        else:
            输出.append((名称, False, f"未支持的点位: {点位}"))
            continue

        if 期望观察:
            通过 = isinstance(r, 观察类结果) and 期望观察 in (r.观察类型 + r.原因)
            输出.append((名称, 通过, f"实际={type(r).__name__} 期望={期望观察}"))
        elif 期望命中 is True:
            通过 = isinstance(r, 命中异常)
            细 = f"实际={type(r).__name__}"
            if 通过:
                细 += f" 严重度={r.默认严重度} 命中依据={r.命中依据[:60]}"
                if 算例.get("预期严重度") and r.默认严重度 != 算例["预期严重度"]:
                    通过 = False
                    细 += f" (期望 S={算例['预期严重度']})"
            输出.append((名称, 通过, 细))
        elif 期望命中 is False:
            通过 = r is None
            输出.append((名称, 通过, f"实际={type(r).__name__ if r else 'None'}"))
        else:
            输出.append((名称, False, "算例无预期值"))

    return 输出


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    print("=" * 70)
    print("R2 异常识别器 · 内置算例自检")
    print("=" * 70)
    for 名称, 通过, 详情 in 跑内置算例():
        icon = "✅" if 通过 else "❌"
        print(f"{icon} {名称}\n   {详情}")
