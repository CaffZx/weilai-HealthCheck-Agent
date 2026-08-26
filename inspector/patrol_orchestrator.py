from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime
from uuid import uuid4

from core.contracts import DataGap, OperatingUnitRef
from core.enums import (
    DataQualityStatus,
    DeliveryStatus,
    InspectionRunStatus,
    TriggerType,
)
from core.inspection_run import InspectionRun
from core.operating_unit import OperatingUnitBinding
from core.result_envelope import InspectionResultEnvelope
from facts.collector import FactCollector
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from inspector.fact_acquisition import FactAcquisition
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from inspector.signal_builder import SignalBuilder
from inspector.signal_reconciler import SignalReconciler
from integrations.category_baseline import MySqlCategoryBaselineService
from integrations.child_fact_async import MySqlChildFactService
from integrations.product_image import MySqlProductImageService
from integrations.repositories import PatrolUnitOfWork
from integrations.runtime_queue import ClaimedJob


class PatrolOrchestrator:
    """最终纯巡检主链路：事实、规则、对账、保存和 Outbox。"""

    def __init__(
        self,
        *,
        engine,
        collector: FactCollector,
        normalizer: FactNormalizer,
        quality_service: FactQualityService,
        snapshot_builder: FactSnapshotBuilder,
        rule_adapter: LegacyRuleAdapter,
        signal_builder: SignalBuilder,
        reconciler: SignalReconciler,
        service_version: str,
        rule_versions: dict[str, str],
        outbox_delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
        category_baseline_service: MySqlCategoryBaselineService | None = None,
        child_fact_service: MySqlChildFactService | None = None,
        product_image_service: MySqlProductImageService | None = None,
    ) -> None:
        self.engine = engine
        self.collector = collector
        self.normalizer = normalizer
        self.quality_service = quality_service
        self.snapshot_builder = snapshot_builder
        self.rule_adapter = rule_adapter
        self.signal_builder = signal_builder
        self.reconciler = reconciler
        self.service_version = service_version
        self.rule_versions = rule_versions
        self.outbox_delivery_status = outbox_delivery_status
        self.category_baseline_service = category_baseline_service
        self.child_fact_service = child_fact_service
        self.product_image_service = product_image_service
        self.acquisition = FactAcquisition(
            engine=engine,
            collector=collector,
            child_fact_service=child_fact_service,
        )

    async def run_job(
        self,
        job: ClaimedJob,
        *,
        as_of: date | None = None,
    ) -> InspectionResultEnvelope:
        binding = OperatingUnitBinding(**job.payload["binding"])
        if binding.operating_unit_id != job.operating_unit_id:
            raise ValueError("job binding does not match operating_unit_id")
        trigger_type = TriggerType(job.payload["trigger_type"])
        current_date = as_of or date.today()
        started_at = datetime.now(UTC)
        run_id = f"pr_{uuid4().hex[:24]}"
        attempt_request_id = "run_request_" + hashlib.sha256(
            f"{job.request_id}\x1f{job.retry_count}".encode()
        ).hexdigest()
        started_run = InspectionRun(
            run_id=run_id,
            job_id=job.job_id,
            batch_id=job.batch_id,
            request_id=attempt_request_id,
            operating_unit_id=job.operating_unit_id,
            trigger_type=trigger_type,
            status=InspectionRunStatus.RECEIVED,
            rule_versions=self.rule_versions,
            started_at=started_at,
        )
        with PatrolUnitOfWork(self.engine) as work:
            assert work.repository is not None
            work.repository.create_run(started_run)
            work.commit()

        try:
            acquired = await self.acquisition.acquire(
                run_id=run_id,
                binding=binding,
                operating_unit_id=job.operating_unit_id,
                reuse_fact_run_id=job.payload.get("reuse_fact_run_id"),
                as_of=current_date,
            )
            current_date = acquired.as_of_date
            raw = acquired.raw
            snapshot_as_of_time = acquired.snapshot_as_of_time
            pending_child_calls = acquired.pending_child_calls
            normalized, source_refs = self.normalizer.normalize(
                binding, raw, as_of=current_date
            )
            if self.product_image_service is not None:
                normalized = self.product_image_service.retain_latest(
                    binding,
                    normalized,
                    observed_at=snapshot_as_of_time,
                )
            if self.category_baseline_service is not None:
                normalized = self.category_baseline_service.observe_and_resolve(
                    binding,
                    normalized,
                    observed_at=snapshot_as_of_time,
                )
            gaps = self.quality_service.check(binding, raw, normalized)
            if pending_child_calls:
                pending_domains = sorted({
                    key.partition(":")[0] for key in pending_child_calls
                })
                missing_asins = sorted({
                    key.partition(":")[2] for key in pending_child_calls
                })
                # 2026-08-22：静默配置命中时不产出该缺口（feature_flags.silenced_gap_codes）。
                if "ASYNC_CHILD_FACT_PENDING" not in self.quality_service.silenced_gap_codes:
                    gaps.append(DataGap(
                        code="ASYNC_CHILD_FACT_PENDING",
                        field="identity.children[].async_facts",
                        impact=(
                            f"{len(missing_asins)} 个子体的前台/扩展数据不可得"
                            f"（如无亚马逊前台详情页），"
                            f"对应点位未判定：{', '.join(missing_asins[:5])}"
                        ),
                        repair_action="部分子 ASIN 无前台详情页，其前台相关异常无法判定；确认子 ASIN 状态后按需处理",
                        blocking=False,
                    ))
            # 已按要求抑制：点位覆盖缺口（inspection.points）与 ASYNC_CHILD_FACT_PENDING
            # 重复，不再对运营告警冗长的点位清单。
            # coverage_gap = self.rule_adapter.coverage_gap(normalized)
            # if coverage_gap is not None:
            #     gaps.append(coverage_gap)
            unit = OperatingUnitRef.derive(
                shop_id=binding.shop_id,
                site_code=binding.site_code,
                parent_asin=binding.parent_asin,
                parent_seller_sku=binding.parent_seller_sku,
            )
            snapshot = self.snapshot_builder.build(
                unit,
                normalized,
                source_refs,
                gaps,
                as_of_time=snapshot_as_of_time,
            )

            anomalies = [] if snapshot.is_blocking else self.rule_adapter.inspect(snapshot)
            candidates = [
                self.signal_builder.build(
                    anomaly=anomaly,
                    snapshot=snapshot,
                    scan_run_id=run_id,
                    initialization_ready=bool(
                        job.payload.get("initialization_ready", True)
                    ),
                )
                for anomaly in anomalies
            ]
            candidates.extend(
                self.signal_builder.build_data_gap_signal(
                    gap=gap,
                    snapshot=snapshot,
                    scan_run_id=run_id,
                )
                for gap in gaps
            )

            finished_at = datetime.now(UTC)
            with PatrolUnitOfWork(self.engine) as work:
                assert work.repository is not None
                existing = work.repository.list_signals_for_update(job.operating_unit_id)
                reconciliation = self.reconciler.reconcile(
                    existing=existing,
                    candidates=candidates,
                    facts_complete=(
                        snapshot.quality_status is DataQualityStatus.COMPLETE
                    ),
                    occurred_at=finished_at,
                    scan_run_id=run_id,
                )
                signals = self.signal_builder.sort(list(reconciliation.signals))
                if snapshot.is_blocking:
                    status = InspectionRunStatus.BLOCKED
                elif snapshot.quality_status is DataQualityStatus.COMPLETE:
                    status = InspectionRunStatus.COMPLETED
                else:
                    status = InspectionRunStatus.COMPLETED_WITH_GAPS
                completed_run = started_run.model_copy(
                    update={
                        "status": status,
                        "fact_snapshot_id": snapshot.snapshot_id,
                        "signal_count": len(signals),
                        "data_gap_count": len(gaps),
                        "finished_at": finished_at,
                    }
                )
                envelope = InspectionResultEnvelope(
                    run=completed_run,
                    signals=signals,
                    handoff_directives=[signal.handoff_directive for signal in signals],
                    producer_versions={"business-inspection": self.service_version},
                    generated_at=finished_at,
                )
                work.repository.persist_reconciled_result(
                    envelope=envelope,
                    snapshot=snapshot,
                    reconciliation=reconciliation,
                    delivery_status=self.outbox_delivery_status,
                )
                work.commit()
            return envelope
        except asyncio.CancelledError:
            failed_run = started_run.model_copy(
                update={
                    "status": InspectionRunStatus.FAILED,
                    "finished_at": datetime.now(UTC),
                    "error_code": "RUN_CANCELLED",
                    "error_message": "patrol run cancelled before completion",
                }
            )
            with PatrolUnitOfWork(self.engine) as work:
                assert work.repository is not None
                work.repository.fail_run(failed_run)
                work.commit()
            raise
        except Exception as exc:
            failed_run = started_run.model_copy(
                update={
                    "status": InspectionRunStatus.FAILED,
                    "finished_at": datetime.now(UTC),
                    "error_code": type(exc).__name__.upper()[:64],
                    "error_message": str(exc),
                }
            )
            with PatrolUnitOfWork(self.engine) as work:
                assert work.repository is not None
                work.repository.fail_run(failed_run)
                work.commit()
            raise
