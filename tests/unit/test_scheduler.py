from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from integrations.rollout import RolloutPolicy
from integrations.scheduler import PatrolScheduler


class FakeLease:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.releases = []
        self.renewals = []

    def acquire(self, **kwargs):
        return self.acquired

    def release(self, **kwargs):
        self.releases.append(kwargs)
        return True

    def renew(self, **kwargs):
        self.renewals.append(kwargs)
        return True


class FakeProvider:
    def __init__(self, units) -> None:
        self.units = units
        self.calls = 0

    async def list_all_active_units(self):
        self.calls += 1
        return self.units


class BlockingProvider(FakeProvider):
    def __init__(self, units, *, delay: float) -> None:
        super().__init__(units)
        self.delay = delay

    async def list_all_active_units(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.units


class FakeQueue:
    engine = object()

    def __init__(self) -> None:
        self.calls = []
        self.active_ids = set()

    def create_batch(self, **kwargs):
        self.calls.append(kwargs)
        return "batch_test", True

    def active_operating_unit_ids(self, operating_unit_ids):
        return self.active_ids & operating_unit_ids


class DueScheduler(PatrolScheduler):
    def __init__(self, *, due_ids, **kwargs):
        super().__init__(**kwargs)
        self.due_ids = due_ids

    def _due_operating_unit_ids(self, now):
        return set(self.due_ids)


@pytest.mark.asyncio
async def test_daily_scheduler_uses_deterministic_idempotency(unit):
    from core.operating_unit import OperatingUnitBinding

    binding = OperatingUnitBinding(
        shop_id=unit.shop_id,
        site_code=unit.site_code,
        parent_asin=unit.parent_asin,
        parent_seller_sku=unit.parent_seller_sku,
        shop_account="test-shop",
    )
    queue = FakeQueue()
    lease = FakeLease()
    scheduler = PatrolScheduler(
        queue=queue,
        unit_provider=FakeProvider([binding]),
        lease=lease,
        holder_id="scheduler-a",
        rule_bundle_version="rules-v1",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    result = await scheduler.create_daily_batch(business_date=date(2026, 7, 31))
    assert result == ("batch_test", True)
    key = queue.calls[0]["idempotency_key"]
    assert key.startswith("DAILY_SCHEDULE:2026-07-31:")
    assert key.endswith(":rules-v1")
    assert lease.releases


@pytest.mark.asyncio
async def test_daily_fallback_reuses_same_business_batch(unit):
    from core.operating_unit import OperatingUnitBinding

    binding = OperatingUnitBinding(
        shop_id=unit.shop_id,
        site_code=unit.site_code,
        parent_asin=unit.parent_asin,
        parent_seller_sku=unit.parent_seller_sku,
        shop_account="test-shop",
    )
    from integrations.runtime_queue import InMemoryRuntimeQueue

    queue = InMemoryRuntimeQueue()
    scheduler = PatrolScheduler(
        queue=queue,
        unit_provider=FakeProvider([binding]),
        lease=FakeLease(),
        holder_id="scheduler-fallback",
        rule_bundle_version="rules-v1",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    primary = await scheduler.create_daily_batch(business_date=date(2026, 8, 3))
    fallback = await scheduler.create_daily_batch(business_date=date(2026, 8, 3))

    assert primary is not None and fallback is not None
    assert primary == (primary[0], True)
    assert fallback == (primary[0], False)
    assert len(queue.jobs_for_batch(primary[0])) == 1


def test_runtime_queue_freezes_owner_set_and_includes_it_in_scope_hash(unit):
    from core.enums import TriggerType
    from core.operating_unit import OperatingUnitBinding
    from integrations.runtime_queue import InMemoryRuntimeQueue, scope_hash

    single_owner = OperatingUnitBinding(
        shop_id=unit.shop_id,
        site_code=unit.site_code,
        parent_asin=unit.parent_asin,
        parent_seller_sku=unit.parent_seller_sku,
        shop_account="test-shop",
        owner_user_ids=(35,),
    )
    multiple_owners = single_owner.with_owner_user_ids((35, 42))
    assert scope_hash([single_owner]) != scope_hash([multiple_owners])

    queue = InMemoryRuntimeQueue()
    batch_id, created = queue.create_batch(
        trigger_type=TriggerType.DAILY_SCHEDULE,
        business_date=date(2026, 8, 4),
        units=[multiple_owners],
        rule_bundle_version="rules-v1",
        idempotency_key="owner-set-test",
    )

    assert created is True
    job = queue.jobs_for_batch(batch_id)[0]
    assert job["payload_json"]["binding"]["owner_user_ids"] == [35, 42]


@pytest.mark.asyncio
async def test_scheduler_without_lease_does_not_query_mcp():
    provider = FakeProvider([])
    scheduler = PatrolScheduler(
        queue=FakeQueue(),
        unit_provider=provider,
        lease=FakeLease(acquired=False),
        holder_id="scheduler-b",
        rule_bundle_version="rules-v1",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    assert await scheduler.create_daily_batch(business_date=date(2026, 7, 31)) is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_scheduler_renews_lease_while_mcp_listing_is_slow(unit):
    from core.operating_unit import OperatingUnitBinding

    binding = OperatingUnitBinding(
        shop_id=unit.shop_id,
        site_code=unit.site_code,
        parent_asin=unit.parent_asin,
        parent_seller_sku=unit.parent_seller_sku,
        shop_account="test-shop",
    )
    lease = FakeLease()
    scheduler = PatrolScheduler(
        queue=FakeQueue(),
        unit_provider=BlockingProvider([binding], delay=0.03),
        lease=lease,
        holder_id="scheduler-heartbeat",
        rule_bundle_version="rules-v1",
        lease_ttl=timedelta(milliseconds=30),
        lease_heartbeat=timedelta(milliseconds=5),
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    assert await scheduler.create_daily_batch(business_date=date(2026, 8, 3))
    assert lease.renewals


@pytest.mark.asyncio
async def test_scheduler_aborts_batch_when_heartbeat_loses_lease(unit):
    from core.operating_unit import OperatingUnitBinding

    class LostLease(FakeLease):
        def renew(self, **kwargs):
            self.renewals.append(kwargs)
            return False

    binding = OperatingUnitBinding(
        shop_id=unit.shop_id,
        site_code=unit.site_code,
        parent_asin=unit.parent_asin,
        parent_seller_sku=unit.parent_seller_sku,
        shop_account="test-shop",
    )
    queue = FakeQueue()
    lease = LostLease()
    scheduler = PatrolScheduler(
        queue=queue,
        unit_provider=BlockingProvider([binding], delay=0.03),
        lease=lease,
        holder_id="scheduler-lost",
        rule_bundle_version="rules-v1",
        lease_ttl=timedelta(milliseconds=30),
        lease_heartbeat=timedelta(milliseconds=5),
        rollout_policy=RolloutPolicy(stage="FULL"),
    )

    assert await scheduler.create_daily_batch(business_date=date(2026, 8, 3)) is None
    assert lease.renewals
    assert queue.calls == []


@pytest.mark.asyncio
async def test_due_scheduler_filters_non_due_and_active_jobs():
    from core.operating_unit import OperatingUnitBinding

    units = [
        OperatingUnitBinding(
            shop_id=100 + index,
            site_code="US",
            parent_asin=f"B0DUE00{index}",
            shop_account=f"shop-{index}",
            parent_seller_sku=f"PARENT-SKU-{index}",
        )
        for index in range(3)
    ]
    queue = FakeQueue()
    queue.active_ids = {units[1].operating_unit_id}
    scheduler = DueScheduler(
        due_ids={units[0].operating_unit_id, units[1].operating_unit_id},
        queue=queue,
        unit_provider=FakeProvider(units),
        lease=FakeLease(),
        holder_id="scheduler-due",
        rule_bundle_version="rules-v1",
        rollout_policy=RolloutPolicy(stage="FULL"),
    )
    result = await scheduler.create_due_batch(
        business_date=date(2026, 7, 31),
        now=datetime(2026, 7, 31, 7, 16, tzinfo=UTC),
    )
    assert result == ("batch_test", True)
    scheduled = queue.calls[0]["units"]
    assert [unit.operating_unit_id for unit in scheduled] == [units[0].operating_unit_id]


def test_rollout_policy_blocks_internal_and_filters_shadow_shop_ids():
    from core.operating_unit import OperatingUnitBinding

    units = [
        OperatingUnitBinding(
            shop_id=shop_id,
            site_code="US",
            parent_asin=f"B0ROLL{shop_id}",
            shop_account=f"shop-{shop_id}",
            parent_seller_sku=f"PARENT-SKU-{shop_id}",
        )
        for shop_id in (101, 202)
    ]
    assert RolloutPolicy(stage="INTERNAL_ONLY").filter_units(units) == []
    assert RolloutPolicy(stage="SHADOW", allowlisted_shop_ids=[202]).filter_units(units) == [
        units[1]
    ]
    assert RolloutPolicy(stage="FULL").filter_units(units) == units


def test_internal_and_shadow_outbox_are_permanently_suppressed():
    from core.enums import DeliveryStatus

    assert RolloutPolicy(stage="INTERNAL_ONLY").outbox_delivery_status is DeliveryStatus.SUPPRESSED
    assert RolloutPolicy(stage="SHADOW").outbox_delivery_status is DeliveryStatus.SUPPRESSED
    assert RolloutPolicy(stage="CANARY").outbox_delivery_status is DeliveryStatus.PENDING
    assert RolloutPolicy(stage="FULL").outbox_delivery_status is DeliveryStatus.PENDING


def test_rollout_external_paths_follow_stage_matrix():
    internal = RolloutPolicy(stage="INTERNAL_ONLY")
    shadow = RolloutPolicy(stage="SHADOW", allowlisted_shop_ids=[101])
    canary = RolloutPolicy(stage="CANARY", allowlisted_shop_ids=[101])
    full = RolloutPolicy(stage="FULL")

    assert not internal.result_delivery_enabled and not internal.feedback_enabled
    assert not shadow.result_delivery_enabled and not shadow.feedback_enabled
    assert canary.result_delivery_enabled and canary.feedback_enabled
    assert full.result_delivery_enabled and full.feedback_enabled
    assert shadow.permits_shop(101) and not shadow.permits_shop(202)
    assert full.permits_shop(202)


@pytest.mark.asyncio
async def test_internal_rollout_does_not_query_mcp_even_with_scheduler_called():
    provider = FakeProvider([])
    scheduler = PatrolScheduler(
        queue=FakeQueue(),
        unit_provider=provider,
        lease=FakeLease(),
        holder_id="scheduler-internal",
        rule_bundle_version="rules-v1",
        rollout_policy=RolloutPolicy(stage="INTERNAL_ONLY"),
    )
    assert await scheduler.create_daily_batch(business_date=date(2026, 8, 3)) is None
    assert provider.calls == 0
