"""事实快照冻结。

改造方案 v1.0 §9。

【content_hash 是整条链路的锚点】
  经营模式 Agent、广告 Agent 和中控都用它校验"大家看的是同一份事实"。
  哈希输入必须是确定性 JSON（sort_keys + 固定分隔符 + ensure_ascii=False），
  跨语言实现要用同样口径，否则中控算出来的哈希永远对不上。

【快照不可变】
  快照生成后不再修改。复盘要新事实就重新采集生成新快照，
  而不是在旧快照上打补丁 —— 否则"当时基于什么做的判断"就查不清了。
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from core.contracts import (
    DataGap,
    OperatingFactSnapshot,
    OperatingUnitRef,
    SourceRef,
    sha256_json,
)
from core.enums import DataQualityStatus
from facts.quality import FactQualityService

#: 参与哈希的业务域。_meta 不参与 —— 里面有采集时间和血缘，
#: 同一份事实重复采集时它会变，会让哈希失去"同一事实"的语义。
HASHED_DOMAINS: tuple[str, ...] = (
    "identity",
    "sales",
    "traffic",
    "profit",
    "inventory",
    "price",
    "quality",
    "execution_history",
)


class FactSnapshotBuilder:
    """把归一化事实 + 溯源 + 缺口冻结成不可变快照。"""

    def __init__(self, *, snapshot_version: int = 1) -> None:
        self.snapshot_version = snapshot_version

    def build(
        self,
        unit: OperatingUnitRef,
        normalized: dict[str, Any],
        source_refs: list[SourceRef],
        data_gaps: list[DataGap],
        *,
        as_of_time: datetime | None = None,
    ) -> OperatingFactSnapshot:
        quality_status, completeness = FactQualityService.summarize(data_gaps)

        body = {
            "operating_unit": unit.model_dump(mode="json"),
            "domains": {
                domain: normalized.get(domain, {}) for domain in HASHED_DOMAINS
            },
            "source_content_hashes": sorted(
                ref.content_hash for ref in source_refs if ref.content_hash
            ),
        }

        return OperatingFactSnapshot(
            snapshot_id=f"fs_{uuid4().hex[:24]}",
            version=self.snapshot_version,
            operating_unit=unit,
            as_of_time=as_of_time or datetime.now(UTC),
            content_hash=sha256_json(body),
            quality_status=quality_status,
            completeness_score=completeness,
            identity=normalized.get("identity", {}),
            sales=normalized.get("sales", {}),
            traffic=normalized.get("traffic", {}),
            profit=normalized.get("profit", {}),
            inventory=normalized.get("inventory", {}),
            price=normalized.get("price", {}),
            quality=normalized.get("quality", {}),
            execution_history=normalized.get("execution_history", {}),
            source_refs=source_refs,
            data_gaps=data_gaps,
        )

    @staticmethod
    def recompute_hash(snapshot: OperatingFactSnapshot) -> str:
        """复算哈希。用于校验快照在传输途中没被改过。"""
        body = {
            "operating_unit": snapshot.operating_unit.model_dump(mode="json"),
            "domains": {
                domain: getattr(snapshot, domain) for domain in HASHED_DOMAINS
            },
            "source_content_hashes": sorted(
                ref.content_hash for ref in snapshot.source_refs if ref.content_hash
            ),
        }
        return sha256_json(body)

    @classmethod
    def verify(cls, snapshot: OperatingFactSnapshot) -> bool:
        return cls.recompute_hash(snapshot) == snapshot.content_hash


def blocked_snapshot(
    unit: OperatingUnitRef,
    data_gaps: list[DataGap],
    *,
    as_of: date | None = None,
) -> OperatingFactSnapshot:
    """核心事实全失败时的最小快照。

    仍然是一个合法快照（有 ID、有哈希、有缺口清单），
    这样中控收到的是"数据阻断包"而不是一个空对象或异常。
    """
    body = {"operating_unit": unit.model_dump(mode="json"), "domains": {}, "blocked": True}
    return OperatingFactSnapshot(
        snapshot_id=f"fs_{uuid4().hex[:24]}",
        operating_unit=unit,
        as_of_time=datetime.now(UTC),
        content_hash=sha256_json(body),
        quality_status=DataQualityStatus.FAILED,
        completeness_score=0.0,
        data_gaps=data_gaps,
        execution_history={"as_of": (as_of or date.today()).isoformat()},
    )
