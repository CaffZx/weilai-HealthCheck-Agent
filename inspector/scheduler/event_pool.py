"""事件池管理

【业务边界】
输入：R2 命中异常 + R3 严重度 + R4 打分结果
输出：事件的创建/复发识别/状态流转/关闭
     全生命周期落库 event_pool + event_state_log

【规则来源】
- docs/业务巡检Agent-代码实现版.md §5（升降级与解除）
- docs/业务巡检Agent_巡检频次最终方案.md §9（异常事件复查频次）

【设计约定】
1. 事件唯一识别 = hash(店铺+父ASIN+问题点位+命中变体)
2. 同一事件二次命中 = 复发（不新建、只更新最近命中时间+复发次数+状态回滚到"新发现"）
3. 状态流转必留痕：写 event_pool 时同步写 event_state_log
4. 10 态状态机严格约束（在 状态_合法转换 里定义）
5. 数据库操作全部走 with sqlite3.connect(...) 保证连接关闭
"""
from __future__ import annotations
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "local" / "healthcheck.db"


# -----------------------------------------------------------------------------
# 10 态状态机（§5）
# -----------------------------------------------------------------------------
状态_全部 = [
    "新发现", "待确认", "处理中", "待观察", "长期跟进",
    "已处理待复扫", "已关闭", "误报", "忽略", "人工中断",
]

# 状态合法转换：{当前: [下一步可选]}
状态_合法转换: dict[str, list[str]] = {
    "新发现":       ["待确认", "处理中", "待观察", "长期跟进", "已处理待复扫", "误报", "忽略", "人工中断", "新发现"],
    "待确认":       ["处理中", "待观察", "长期跟进", "已处理待复扫", "误报", "忽略", "人工中断"],
    "处理中":       ["已处理待复扫", "待观察", "长期跟进", "误报", "忽略", "人工中断"],
    "待观察":       ["处理中", "已处理待复扫", "已关闭", "长期跟进", "误报", "忽略", "人工中断", "新发现"],
    "长期跟进":     ["处理中", "已处理待复扫", "已关闭", "误报", "忽略", "人工中断", "新发现"],
    "已处理待复扫": ["待观察", "已关闭", "误报", "忽略", "人工中断", "新发现"],
    "已关闭":       ["新发现"],             # 关闭后再次命中即复发
    "误报":         ["新发现"],
    "忽略":         ["新发现"],
    "人工中断":     [s for s in 状态_全部 if s != "人工中断"],
}

关闭态 = {"已关闭", "误报", "忽略"}


# -----------------------------------------------------------------------------
# 数据契约
# -----------------------------------------------------------------------------
@dataclass
class 事件命中输入:
    """R2 命中 + R3 判定 + R4 打分 完成后的输入。"""
    店铺账号: str
    父ASIN: str
    问题点位: str
    作用层级: str
    异常类型: str                       # '现象即原因型' | '表现型'
    严重度: str                         # 'S0' | 'S1' | 'S2'
    判定依据: dict                      # {判定过程, 触发字段, 判定日志}
    站点: str | None = None
    父SKU: str | None = None
    异常大类: str | None = None
    命中变体: str | None = None
    变体重要性: str | None = None
    是否共因上调: bool = False
    初判严重度: str | None = None
    单异常执行分数: float | None = None
    参数版本: str | None = None
    巡检批次: str | None = None


@dataclass
class 事件处理结果:
    """事件入池/复发/更新完成后的返回。"""
    id: int
    唯一识别: str
    动作: str                            # 'created' | 'reappeared' | 'updated' | 'unchanged'
    当前状态: str
    严重度: str | None
    复发次数: int


# -----------------------------------------------------------------------------
# 唯一识别生成
# -----------------------------------------------------------------------------
def 生成唯一识别(店铺账号: str, 父ASIN: str, 问题点位: str, 命中变体: str | None) -> str:
    """§5.0 定义的唯一识别：店铺+父ASIN+问题点位+命中变体。

    命中变体为 None 时用 空串 参与 hash，保证同一父ASIN链接级异常只有一个事件。
    """
    key = f"{店铺账号}|{父ASIN}|{问题点位}|{命中变体 or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# -----------------------------------------------------------------------------
# DB 工具
# -----------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")   # 批巡多线程写事件池，避免立即 database is locked
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------------------------------------------------------
# 状态流转
# -----------------------------------------------------------------------------
def 校验状态转换(当前: str, 下一步: str) -> tuple[bool, str]:
    """按 状态_合法转换 表校验；返回 (是否合法, 说明)。"""
    if 当前 == 下一步:
        return True, "状态未变"
    合法 = 状态_合法转换.get(当前, [])
    if 下一步 in 合法:
        return True, f"合法转换 {当前} → {下一步}"
    return False, f"非法转换 {当前} → {下一步}（合法目标: {合法}）"


def _写状态日志(conn: sqlite3.Connection, event_id: int, 变更前状态: str, 变更后状态: str,
              变更前严重度: str | None, 变更后严重度: str | None,
              变更类型: str, 变更原因: str, 操作人: str = "系统",
              上下文: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO event_state_log
        (event_id, 变更前状态, 变更后状态, 变更前严重度, 变更后严重度,
         变更类型, 变更原因, 操作人, 上下文)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, 变更前状态, 变更后状态, 变更前严重度, 变更后严重度,
         变更类型, 变更原因, 操作人, json.dumps(上下文 or {}, ensure_ascii=False)),
    )


# -----------------------------------------------------------------------------
# 主接口：事件入池（新建 or 复发）
# -----------------------------------------------------------------------------
def 处理命中事件(输入: 事件命中输入) -> 事件处理结果:
    """核心入口：R2/R3/R4 完成后调用，事件入池 + 状态自动流转。

    行为：
    - 找不到既有事件 → 创建，状态 = 新发现
    - 找到既有开放事件（非关闭态）→ 更新最近命中时间+严重度；状态不变（除非严重度升）
    - 找到既有关闭态事件（已关闭/误报/忽略）→ 复发：新建一条同唯一识别，状态='新发现'；旧事件保留供追溯
      * 简化实现：本版对同一唯一识别只保留一条记录，复发时改状态并 复发次数++
    """
    唯一识别 = 生成唯一识别(输入.店铺账号, 输入.父ASIN, 输入.问题点位, 输入.命中变体)
    with _conn() as conn:
        既有 = conn.execute(
            "SELECT id, 当前状态, 严重度, 复发次数, 首次命中时间 FROM event_pool WHERE 唯一识别=?",
            (唯一识别,),
        ).fetchone()

        if 既有 is None:
            # 新建
            cur = conn.execute(
                """INSERT INTO event_pool
                (唯一识别, 店铺账号, 站点, 父ASIN, 父SKU,
                 异常大类, 问题点位, 作用层级, 命中变体, 变体重要性, 异常类型,
                 严重度, 是否共因上调, 初判严重度, 单异常执行分数,
                 当前状态, 判定依据, 最近巡检批次, 参数版本)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '新发现', ?, ?, ?)""",
                (唯一识别, 输入.店铺账号, 输入.站点, 输入.父ASIN, 输入.父SKU,
                 输入.异常大类, 输入.问题点位, 输入.作用层级, 输入.命中变体, 输入.变体重要性, 输入.异常类型,
                 输入.严重度, 1 if 输入.是否共因上调 else 0, 输入.初判严重度, 输入.单异常执行分数,
                 json.dumps(输入.判定依据, ensure_ascii=False, default=str),
                 输入.巡检批次, 输入.参数版本),
            )
            event_id = cur.lastrowid
            _写状态日志(conn, event_id, None, "新发现", None, 输入.严重度,
                     变更类型="状态流转", 变更原因=f"新建事件（{输入.问题点位}）",
                     上下文={"批次": 输入.巡检批次})
            conn.commit()
            log.info("事件入池-新建 id=%s 唯一识别=%s 点位=%s 严重度=%s",
                    event_id, 唯一识别, 输入.问题点位, 输入.严重度)
            return 事件处理结果(id=event_id, 唯一识别=唯一识别, 动作="created",
                             当前状态="新发现", 严重度=输入.严重度, 复发次数=0)

        event_id = 既有["id"]
        旧状态 = 既有["当前状态"]
        旧严重度 = 既有["严重度"]
        复发次数 = 既有["复发次数"]

        if 旧状态 in 关闭态:
            # 复发
            新复发数 = 复发次数 + 1
            conn.execute(
                """UPDATE event_pool SET
                当前状态='新发现',
                严重度=?,
                是否共因上调=?,
                初判严重度=?,
                单异常执行分数=?,
                最近命中时间=?,
                复发次数=?,
                关闭时间=NULL, 关闭原因=NULL,
                判定依据=?,
                最近巡检批次=?,
                参数版本=?,
                更新时间=?
                WHERE id=?""",
                (输入.严重度, 1 if 输入.是否共因上调 else 0, 输入.初判严重度, 输入.单异常执行分数,
                 _now(), 新复发数,
                 json.dumps(输入.判定依据, ensure_ascii=False, default=str),
                 输入.巡检批次, 输入.参数版本, _now(), event_id),
            )
            _写状态日志(conn, event_id, 旧状态, "新发现", 旧严重度, 输入.严重度,
                     变更类型="状态流转", 变更原因=f"复发（第 {新复发数} 次），从 {旧状态} 回到新发现",
                     上下文={"批次": 输入.巡检批次})
            conn.commit()
            log.info("事件复发 id=%s 唯一识别=%s 复发次数=%s", event_id, 唯一识别, 新复发数)
            return 事件处理结果(id=event_id, 唯一识别=唯一识别, 动作="reappeared",
                             当前状态="新发现", 严重度=输入.严重度, 复发次数=新复发数)

        # 开放态 → 更新
        严重度变化 = 输入.严重度 != 旧严重度
        conn.execute(
            """UPDATE event_pool SET
            严重度=?, 是否共因上调=?, 初判严重度=?, 单异常执行分数=?,
            最近命中时间=?,
            判定依据=?, 最近巡检批次=?, 参数版本=?, 更新时间=?
            WHERE id=?""",
            (输入.严重度, 1 if 输入.是否共因上调 else 0, 输入.初判严重度, 输入.单异常执行分数,
             _now(),
             json.dumps(输入.判定依据, ensure_ascii=False, default=str),
             输入.巡检批次, 输入.参数版本, _now(), event_id),
        )
        if 严重度变化:
            变更类型 = "auto升级" if _严重度序(输入.严重度) < _严重度序(旧严重度) else "auto降级"
            _写状态日志(conn, event_id, 旧状态, 旧状态, 旧严重度, 输入.严重度,
                     变更类型=变更类型, 变更原因=f"重扫严重度 {旧严重度} → {输入.严重度}",
                     上下文={"批次": 输入.巡检批次})
        conn.commit()
        log.info("事件更新 id=%s 唯一识别=%s 状态=%s 严重度=%s→%s",
                event_id, 唯一识别, 旧状态, 旧严重度, 输入.严重度)
        return 事件处理结果(id=event_id, 唯一识别=唯一识别,
                         动作="updated" if 严重度变化 else "unchanged",
                         当前状态=旧状态, 严重度=输入.严重度, 复发次数=复发次数)


def _严重度序(s: str | None) -> int:
    return {"S0": 0, "S1": 1, "S2": 2}.get(s or "", 99)


# -----------------------------------------------------------------------------
# 状态流转（人工/自动）
# -----------------------------------------------------------------------------
def 流转状态_事务(conn: sqlite3.Connection, event_id: int, 下一步状态: str, 变更原因: str,
              操作人: str = "系统", 变更类型: str = "状态流转",
              关闭原因: str | None = None, 上下文: dict | None = None) -> tuple[bool, str]:
    """在调用方事务中流转状态，供工作台把状态、动作和日志原子写入。"""
    既有 = conn.execute(
        "SELECT id, 当前状态, 严重度 FROM event_pool WHERE id=?", (event_id,)
    ).fetchone()
    if not 既有:
        return False, "事件不存在"

    旧状态 = 既有["当前状态"]
    合法, 说明 = 校验状态转换(旧状态, 下一步状态)
    if not 合法:
        return False, 说明

    更新参数 = [下一步状态, _now(), event_id]
    更新SQL = "UPDATE event_pool SET 当前状态=?, 更新时间=?"
    if 下一步状态 in 关闭态:
        更新SQL += ", 关闭时间=?, 关闭原因=?"
        更新参数 = [下一步状态, _now(), _now(), 关闭原因 or 变更原因, event_id]
    更新SQL += " WHERE id=?"
    conn.execute(更新SQL, tuple(更新参数))
    _写状态日志(conn, event_id, 旧状态, 下一步状态, 既有["严重度"], 既有["严重度"],
             变更类型=变更类型, 变更原因=变更原因, 操作人=操作人, 上下文=上下文)
    log.info("事件状态流转 id=%s %s → %s (%s)", event_id, 旧状态, 下一步状态, 变更原因)
    return True, 说明


def 流转状态(event_id: int, 下一步状态: str, 变更原因: str,
             操作人: str = "系统", 变更类型: str = "状态流转",
             关闭原因: str | None = None) -> bool:
    """通用状态流转。返回是否成功。"""
    with _conn() as conn:
        成功, 说明 = 流转状态_事务(
            conn, event_id, 下一步状态, 变更原因, 操作人, 变更类型, 关闭原因,
        )
        if not 成功:
            log.warning("流转状态失败 id=%s %s", event_id, 说明)
            return False
        conn.commit()
        return True


def 关闭事件(event_id: int, 关闭原因: str, 操作人: str = "系统") -> bool:
    """便捷函数：关闭事件（走 已关闭 状态）。"""
    return 流转状态(event_id, "已关闭", 变更原因=关闭原因, 操作人=操作人,
                变更类型="解除关闭", 关闭原因=关闭原因)


# -----------------------------------------------------------------------------
# 查询接口
# -----------------------------------------------------------------------------
def 查开放事件(父ASIN: str) -> list[dict]:
    """查某父ASIN下所有未关闭的事件（用于父卡归集）。"""
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM event_pool
            WHERE 父ASIN=? AND 当前状态 NOT IN ('已关闭','误报','忽略')
            ORDER BY 单异常执行分数 DESC""",
            (父ASIN,),
        ).fetchall()
    return [dict(r) for r in rows]


def 查事件历史(event_id: int) -> list[dict]:
    """查某事件的完整状态流转历史（供任务卡'上次处理记录'字段用）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM event_state_log WHERE event_id=? ORDER BY 变更时间",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def 统计():
    with _conn() as conn:
        s = {}
        for 状态 in 状态_全部:
            n = conn.execute(
                "SELECT COUNT(*) FROM event_pool WHERE 当前状态=?", (状态,)
            ).fetchone()[0]
            s[状态] = n
        s["_总计"] = conn.execute("SELECT COUNT(*) FROM event_pool").fetchone()[0]
    return s


def 清空_仅测试用():
    """危险操作：清空 event_pool 和 event_state_log。仅供测试。"""
    with _conn() as conn:
        conn.execute("DELETE FROM event_state_log")
        conn.execute("DELETE FROM event_pool")
        conn.commit()
    log.warning("event_pool 和 event_state_log 已清空")


# -----------------------------------------------------------------------------
# 单元测试：完整生命周期演练
# -----------------------------------------------------------------------------
def 跑内置算例() -> list[tuple[str, bool, str]]:
    """从零开始跑完整生命周期：新建 → 更新 → 关闭 → 复发。"""
    # 保证 schema 存在
    from data import local_store
    local_store.init_db()

    输出 = []
    清空_仅测试用()

    # 1. 新建
    输入1 = 事件命中输入(
        店铺账号="am_example_us", 父ASIN="B0EXAMPLE01",
        问题点位="FBA可售库存为0", 作用层级="变体级", 异常类型="现象即原因型",
        严重度="S0", 命中变体="B0X_黑色/M", 变体重要性="主要色",
        判定依据={"命中依据": "FBA可售库存=0"},
        单异常执行分数=100.0, 巡检批次="20260709-1000", 参数版本="R2v1|R3v1|R4v1",
    )
    r1 = 处理命中事件(输入1)
    输出.append(("1_新建", r1.动作 == "created" and r1.当前状态 == "新发现",
              f"动作={r1.动作} 状态={r1.当前状态}"))

    # 2. 再次命中（严重度不变）→ unchanged
    r2 = 处理命中事件(输入1)
    输出.append(("2_同状态再命中", r2.动作 == "unchanged",
              f"动作={r2.动作} 状态={r2.当前状态}"))

    # 3. 严重度升级 S0 → S0（不变，仍 unchanged）；换个例子测严重度变化
    输入3 = 事件命中输入(**{**输入1.__dict__, "严重度": "S1"})
    r3 = 处理命中事件(输入3)
    输出.append(("3_严重度降级S0→S1", r3.动作 == "updated" and r3.严重度 == "S1",
              f"动作={r3.动作} 严重度={r3.严重度}"))

    # 4. 状态流转：新发现 → 处理中
    ok = 流转状态(r1.id, "处理中", 变更原因="运营开始处理", 操作人="张三")
    输出.append(("4_流转到处理中", ok, "..."))

    # 5. 非法流转（处理中 → 待确认）应失败
    ok = 流转状态(r1.id, "待确认", 变更原因="故意非法")
    输出.append(("5_非法流转应失败", not ok, "..."))

    # 6. 处理完成：处理中 → 已处理待复扫
    ok = 流转状态(r1.id, "已处理待复扫", 变更原因="补货已到")
    输出.append(("6_流转到已处理待复扫", ok, "..."))

    # 7. 关闭事件
    ok = 关闭事件(r1.id, 关闭原因="复扫确认库存恢复")
    输出.append(("7_关闭事件", ok, "..."))

    # 8. 复发：关闭后再次命中 → reappeared
    r8 = 处理命中事件(输入1)
    输出.append(("8_复发", r8.动作 == "reappeared" and r8.复发次数 == 1 and r8.当前状态 == "新发现",
              f"动作={r8.动作} 复发次数={r8.复发次数} 状态={r8.当前状态}"))

    # 9. 查历史应该有 5+ 条流转记录（新建/降级/处理中/已处理/已关闭/复发...）
    历史 = 查事件历史(r1.id)
    输出.append(("9_历史留痕", len(历史) >= 5, f"共 {len(历史)} 条记录"))

    # 10. 开放事件查询
    开放 = 查开放事件("B0EXAMPLE01")
    输出.append(("10_查开放事件", len(开放) == 1, f"{len(开放)} 个开放事件"))

    return 输出


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    print("=" * 70)
    print("事件池 · 完整生命周期演练")
    print("=" * 70)
    for 名称, 通过, 详情 in 跑内置算例():
        icon = "✅" if 通过 else "❌"
        print(f"{icon} {名称}\n   {详情}")
    print()
    print("=== 最终统计 ===")
    for k, v in 统计().items():
        print(f"  {k}: {v}")
