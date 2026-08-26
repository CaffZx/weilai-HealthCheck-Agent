from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from core.contracts import DataGap, OperatingFactSnapshot, OperatingUnitRef, SourceRef
from core.control_center_contracts import (
    CriterionResult,
    ReviewCriterion,
    ReviewRequest,
    ReviewResult,
)
from core.enums import DataQualityStatus
from facts.collector import FactCollector
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from facts.snapshot import FactSnapshotBuilder
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from integrations.category_baseline import MySqlCategoryBaselineService
from integrations.child_fact_async import MySqlChildFactService
from integrations.product_image import MySqlProductImageService
from integrations.repositories.tables import (
    patrol_fact_snapshot,
    patrol_operating_unit_catalog,
    patrol_review,
    patrol_sys_user,
)


class ReviewConflict(RuntimeError):
    pass


class ReviewStore(Protocol):
    def load_baseline(self, snapshot_id: str, operating_unit_id: str) -> OperatingFactSnapshot: ...
    def get_result(self, request_id: str, review_round: int, request_hash: str) -> ReviewResult | None: ...
    def start(self, request: ReviewRequest) -> bool: ...
    def complete(self, request: ReviewRequest, result: ReviewResult) -> None: ...
    def fail(self, request: ReviewRequest, error: Exception) -> None: ...
    def resolve_owner_names(self, owner_user_ids: list[int]) -> dict[int, str]: ...
    def load_owner_user_ids(self, operating_unit_id: str) -> tuple[int, ...]: ...


def _utc_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class MySqlReviewStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def resolve_owner_names(self, owner_user_ids: list[int]) -> dict[int, str]:
        if not owner_user_ids:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    patrol_sys_user.c.user_id,
                    patrol_sys_user.c.user_name,
                ).where(patrol_sys_user.c.user_id.in_(owner_user_ids))
            ).mappings()
            return {
                int(row["user_id"]): str(row["user_name"])
                for row in rows
                if str(row["user_name"] or "").strip()
            }

    def load_owner_user_ids(self, operating_unit_id: str) -> tuple[int, ...]:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json).where(
                    patrol_operating_unit_catalog.c.operating_unit_id
                    == operating_unit_id
                )
            ).scalar_one_or_none()
        owner_ids = {
            int(value)
            for value in (row or [])
            if str(value).isdigit() and int(value) > 0
        }
        return tuple(sorted(owner_ids))

    def load_baseline(self, snapshot_id: str, operating_unit_id: str) -> OperatingFactSnapshot:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(patrol_fact_snapshot).where(
                    patrol_fact_snapshot.c.snapshot_id == snapshot_id,
                    patrol_fact_snapshot.c.operating_unit_id == operating_unit_id,
                )
            ).mappings().one_or_none()
        if row is None:
            raise ValueError("baseline snapshot is missing or belongs to another operating unit")
        normalized = dict(row["normalized_summary_json"])
        source_refs = [SourceRef.model_validate(item) for item in row["source_refs_json"]]
        data_gaps = [DataGap.model_validate(item) for item in row["data_gaps_json"]]
        identity = dict(normalized.get("identity") or {})
        unit = OperatingUnitRef.derive(
            shop_id=identity.get("shop_id"),
            site_code=identity.get("site_code"),
            parent_asin=identity.get("parent_asin"),
            parent_seller_sku=identity.get("parent_seller_sku"),
        )
        return OperatingFactSnapshot(
            snapshot_id=row["snapshot_id"],
            operating_unit=unit,
            as_of_time=datetime.combine(row["as_of"], datetime.min.time(), tzinfo=UTC),
            content_hash=f"sha256:{row['content_hash']}",
            quality_status=DataQualityStatus(row["quality_status"]),
            completeness_score=float(row["completeness_score"] or 0) / 100,
            source_refs=source_refs,
            data_gaps=data_gaps,
            **{name: dict(normalized.get(name) or {}) for name in (
                "identity", "sales", "traffic", "profit", "inventory", "price",
                "quality", "execution_history",
            )},
        )

    def get_result(
        self,
        request_id: str,
        review_round: int,
        request_hash: str,
    ) -> ReviewResult | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(patrol_review).where(
                    patrol_review.c.review_request_id == request_id,
                    patrol_review.c.review_round == review_round,
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        if row["request_hash"] != request_hash.removeprefix("sha256:"):
            raise ReviewConflict(
                "reviewRequestId and reviewRound identify one immutable review request; "
                "requestedAt changes are tolerated, but retry with the original "
                "approvedPlan, executionReceipts, and criteria, or increment reviewRound "
                "before submitting changed business content"
            )
        if row["status"] == "COMPLETED" and row["result_json"]:
            return ReviewResult.model_validate(row["result_json"])
        if row["status"] == "PROCESSING":
            raise ReviewConflict("review request is already processing")
        return None

    def start(self, request: ReviewRequest) -> bool:
        try:
            with self.engine.begin() as connection:
                connection.execute(sa.insert(patrol_review).values(
                    review_request_id=request.review_request_id,
                    review_round=request.review_round,
                    request_hash=request.request_hash().removeprefix("sha256:"),
                    patrol_batch_no=request.patrol_batch_no,
                    proposal_id=request.proposal_id,
                    operating_unit_id=request.binding.operating_unit_id,
                    baseline_snapshot_id=request.baseline_snapshot_id,
                    request_json=request.model_dump(mode="json", by_alias=True),
                    status="PROCESSING",
                    requested_at=request.requested_at.astimezone(UTC).replace(tzinfo=None),
                ))
        except IntegrityError:
            with self.engine.begin() as connection:
                updated = connection.execute(
                    sa.update(patrol_review).where(
                        patrol_review.c.review_request_id == request.review_request_id,
                        patrol_review.c.review_round == request.review_round,
                        patrol_review.c.request_hash
                        == request.request_hash().removeprefix("sha256:"),
                        patrol_review.c.status == "FAILED",
                    ).values(
                        status="PROCESSING",
                        error_message=None,
                        completed_at=None,
                    )
                )
            return updated.rowcount == 1
        return True

    def complete(self, request: ReviewRequest, result: ReviewResult) -> None:
        with self.engine.begin() as connection:
            updated = connection.execute(
                sa.update(patrol_review).where(
                    patrol_review.c.review_request_id == request.review_request_id,
                    patrol_review.c.review_round == request.review_round,
                    patrol_review.c.request_hash == request.request_hash().removeprefix("sha256:"),
                    patrol_review.c.status == "PROCESSING",
                ).values(
                    status="COMPLETED",
                    current_snapshot_id=result.current_fact_snapshot.snapshot_id,
                    current_snapshot_json=result.current_fact_snapshot.model_dump(mode="json"),
                    outcome_status=result.outcome_status,
                    result_json=result.model_dump(mode="json", by_alias=True),
                    completed_at=result.reviewed_at.astimezone(UTC).replace(tzinfo=None),
                    error_message=None,
                )
            )
        if updated.rowcount != 1:
            raise ReviewConflict("review ownership changed before result commit")

    def fail(self, request: ReviewRequest, error: Exception) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                sa.update(patrol_review).where(
                    patrol_review.c.review_request_id == request.review_request_id,
                    patrol_review.c.review_round == request.review_round,
                    patrol_review.c.status == "PROCESSING",
                ).values(status="FAILED", error_message=type(error).__name__[:2000])
            )


def _path(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _criterion_result(snapshot: dict[str, Any], criterion: ReviewCriterion) -> CriterionResult:
    actual = _path(snapshot, criterion.metric_path)
    target = criterion.target
    if actual is None:
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            metric_path=criterion.metric_path,
            operator=criterion.operator,
            target=target,
            actual=None,
            evaluation_status="NOT_EVALUATED",
            missing_reason=f"复盘事实缺少字段 {criterion.metric_path}",
            met=None,
        )
    met = bool(
        criterion.operator == "GTE" and actual >= target
        or criterion.operator == "LTE" and actual <= target
        or criterion.operator == "EQ" and actual == target
    )
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        metric_path=criterion.metric_path,
        operator=criterion.operator,
        target=target,
        actual=actual,
        evaluation_status="MET" if met else "NOT_MET",
        missing_reason=None,
        met=met,
    )


class ReviewService:
    def __init__(
        self,
        *,
        store: ReviewStore,
        collector: FactCollector,
        normalizer: FactNormalizer,
        quality_service: FactQualityService,
        snapshot_builder: FactSnapshotBuilder,
        rule_adapter: LegacyRuleAdapter | None = None,
        category_baseline_service: MySqlCategoryBaselineService | None = None,
        child_fact_service: MySqlChildFactService | None = None,
        product_image_service: MySqlProductImageService | None = None,
    ) -> None:
        self.store = store
        self.collector = collector
        self.normalizer = normalizer
        self.quality_service = quality_service
        self.snapshot_builder = snapshot_builder
        self.rule_adapter = rule_adapter or LegacyRuleAdapter()
        self.category_baseline_service = category_baseline_service
        self.child_fact_service = child_fact_service
        self.product_image_service = product_image_service

    async def review(self, request: ReviewRequest, *, as_of: date | None = None) -> ReviewResult:
        cached = self.store.get_result(
            request.review_request_id, request.review_round, request.request_hash()
        )
        if cached is not None:
            return cached
        if not self.store.start(request):
            cached = self.store.get_result(
                request.review_request_id, request.review_round, request.request_hash()
            )
            if cached is not None:
                return cached
            raise ReviewConflict("review request could not be claimed")
        try:
            binding = request.binding
            load_owner_user_ids = getattr(self.store, "load_owner_user_ids", None)
            owner_ids = (
                load_owner_user_ids(binding.operating_unit_id)
                if load_owner_user_ids is not None
                else ()
            )
            if owner_ids:
                binding = binding.with_owner_user_ids(owner_ids)
            baseline = self.store.load_baseline(
                request.baseline_snapshot_id, binding.operating_unit_id
            )
            current_date = as_of or date.today()
            raw = await self.collector.collect(binding, current_date)
            pending_child_calls: dict[str, tuple[str, dict]] = {}
            if self.child_fact_service is not None:
                self.child_fact_service.enqueue_missing(
                    binding,
                    self.collector,
                    raw.get("product_info"),
                    current_date,
                )
                cached_child_results = self.child_fact_service.load_fresh_results(
                    binding.operating_unit_id
                )
                raw.update(cached_child_results)
                expected_child_calls = self.collector.build_child_calls(
                    binding,
                    current_date,
                    raw.get("product_info"),
                )
                pending_child_calls = {
                    key: value
                    for key, value in expected_child_calls.items()
                    if key not in cached_child_results
                }
            normalized, source_refs = self.normalizer.normalize(
                binding, raw, as_of=current_date
            )
            if self.product_image_service is not None:
                normalized = self.product_image_service.retain_latest(
                    binding, normalized
                )
            if self.category_baseline_service is not None:
                normalized = self.category_baseline_service.observe_and_resolve(
                    binding,
                    normalized,
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
                            f"{len(missing_asins)} 个子体的异步快照尚未就绪"
                            f"（域：{', '.join(pending_domains)}），复盘对应指标未判定"
                        ),
                        repair_action="等待子体事实 Worker 完成低并发采集后重新复盘",
                        blocking=False,
                    ))
            # 与定时巡检一致：有数据才判，无数据不报点位覆盖缺口（目标类缺口仍走 quality.check）。
            # coverage_gap = self.rule_adapter.coverage_gap(normalized)
            # if coverage_gap is not None:
            #     gaps.append(coverage_gap)
            current = self.snapshot_builder.build(
                OperatingUnitRef.derive(
                    shop_id=binding.shop_id,
                    site_code=binding.site_code,
                    parent_asin=binding.parent_asin,
                    parent_seller_sku=binding.parent_seller_sku,
                ),
                normalized,
                source_refs,
                gaps,
            )
            owner_user_name = self._enrich_owner_name(current)
            current_json = current.model_dump(mode="json")
            success = [_criterion_result(current_json, item) for item in request.success_criteria]
            failure = [_criterion_result(current_json, item) for item in request.failure_criteria]
            deviations = self._execution_deviations(
                request.approved_plan, request.execution_receipts
            )
            criteria = [*success, *failure]
            if current.is_blocking or any(
                item.evaluation_status == "NOT_EVALUATED" for item in criteria
            ):
                outcome = "INCONCLUSIVE"
            elif any(item.met for item in failure):
                outcome = "FAILED"
            elif success and all(item.met for item in success) and not deviations:
                outcome = "SUCCESS"
            elif any(item.met for item in success):
                outcome = "PARTIAL_SUCCESS"
            else:
                outcome = "FAILED"
            result = ReviewResult(
                review_request_id=request.review_request_id,
                review_round=request.review_round,
                patrol_batch_no=request.patrol_batch_no,
                proposal_id=request.proposal_id,
                baseline_snapshot_id=baseline.snapshot_id,
                current_fact_snapshot=current,
                owner_user_name=owner_user_name,
                outcome_status=outcome,
                success_results=success,
                failure_results=failure,
                data_gaps=current.data_gaps,
                execution_deviations=deviations,
                requires_new_patrol=outcome == "FAILED",
                next_recommendation=(
                    "REPAIR_DATA" if outcome == "INCONCLUSIVE"
                    else "START_NEW_PATROL_CYCLE" if outcome == "FAILED"
                    else "CLOSE_CYCLE"
                ),
                reviewed_at=datetime.now(UTC),
            )
            self.store.complete(request, result)
            return result
        except Exception as exc:
            self.store.fail(request, exc)
            raise

    def _enrich_owner_name(self, snapshot: OperatingFactSnapshot) -> str | None:
        """用负责人目录补全快照 identity 的负责人姓名，返回复查结果 ownerUserName。"""
        identity = snapshot.identity or {}
        owner_users = identity.get("owner_users")
        if not isinstance(owner_users, list):
            return None
        missing_ids: set[int] = set()
        names: dict[int, str] = {}
        for item in owner_users:
            if not isinstance(item, dict):
                continue
            try:
                user_id = int(item.get("user_id"))
            except (TypeError, ValueError):
                continue
            user_name = str(item.get("user_name") or "").strip()
            if user_id > 0 and user_name:
                names[user_id] = user_name
            elif user_id > 0:
                missing_ids.add(user_id)
        if missing_ids:
            resolved = self.store.resolve_owner_names(sorted(missing_ids))
            for item in owner_users:
                if not isinstance(item, dict):
                    continue
                user_id = item.get("user_id")
                try:
                    resolved_id = int(user_id)
                except (TypeError, ValueError):
                    continue
                if resolved_id in resolved:
                    item["user_name"] = resolved[resolved_id]
                    names[resolved_id] = resolved[resolved_id]
        if len(names) == 1:
            return next(iter(names.values()))
        if names:
            return names[sorted(names)[0]]
        return None

    @staticmethod
    def _execution_deviations(
        approved_plan: dict[str, Any], execution_receipts: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        expected = {
            item.get("planItemId") or item.get("plan_item_id")
            for item in approved_plan.get("items", [])
        }
        effective = {
            item.get("planItemId") or item.get("plan_item_id")
            for item in execution_receipts
            if (item.get("platformStatus") or item.get("platform_status")) == "EFFECTIVE"
        }
        return [
            {"planItemId": item, "code": "ACTION_NOT_EFFECTIVE"}
            for item in sorted(expected - effective)
            if item
        ]
