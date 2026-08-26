from __future__ import annotations

from core.enums import DeliveryStatus
from core.operating_unit import OperatingUnitBinding

ROLLOUT_STAGES = {"INTERNAL_ONLY", "SHADOW", "CANARY", "FULL"}


class RolloutPolicy:
    def __init__(
        self,
        *,
        stage: str = "INTERNAL_ONLY",
        allowlisted_shop_ids: list[int] | None = None,
        result_delivery_enabled: bool | None = None,
        feedback_enabled: bool | None = None,
    ) -> None:
        self.stage = stage.upper()
        if self.stage not in ROLLOUT_STAGES:
            raise ValueError(f"invalid rollout stage: {stage}")
        self.allowlisted_shop_ids = frozenset(allowlisted_shop_ids or [])
        # 显式 feature_flags 覆盖：允许 stage=FULL 但仍单独关闭 result_delivery / feedback
        self._result_delivery_override = result_delivery_enabled
        self._feedback_override = feedback_enabled

    @property
    def patrol_execution_enabled(self) -> bool:
        return self.stage != "INTERNAL_ONLY"

    @property
    def result_delivery_enabled(self) -> bool:
        if self._result_delivery_override is not None:
            return self._result_delivery_override
        return self.stage in {"CANARY", "FULL"}

    @property
    def feedback_enabled(self) -> bool:
        if self._feedback_override is not None:
            return self._feedback_override
        return self.stage in {"CANARY", "FULL"}

    @property
    def allowed_shop_ids(self) -> frozenset[int] | None:
        if self.stage == "FULL":
            return None
        return self.allowlisted_shop_ids

    def permits_shop(self, shop_id: int) -> bool:
        allowed = self.allowed_shop_ids
        return self.patrol_execution_enabled and (
            allowed is None or shop_id in allowed
        )

    @property
    def outbox_delivery_status(self) -> DeliveryStatus:
        # 1) 内部/影子模式一律不投递
        # 2) result_delivery_enabled 关闭时（无论 stage 是什么）也不写 PENDING —— 否则
        #    outbox 会无限累积（历史遗留：曾积压 17271 条 INSPECTION_RUN PENDING）。
        if self.stage in {"INTERNAL_ONLY", "SHADOW"}:
            return DeliveryStatus.SUPPRESSED
        if not self.result_delivery_enabled:
            return DeliveryStatus.SUPPRESSED
        return DeliveryStatus.PENDING

    @property
    def control_center_delivery_status(self) -> DeliveryStatus:
        # CC 管道（CONTROL_CENTER_PATROL_BATCH）只受 stage 控制，不受
        # result_delivery_enabled 影响 —— 后者是 INSPECTION_RUN 老管道的开关。
        if self.stage in {"INTERNAL_ONLY", "SHADOW"}:
            return DeliveryStatus.SUPPRESSED
        return DeliveryStatus.PENDING

    def filter_units(
        self, units: list[OperatingUnitBinding]
    ) -> list[OperatingUnitBinding]:
        return [unit for unit in units if self.permits_shop(unit.shop_id)]
