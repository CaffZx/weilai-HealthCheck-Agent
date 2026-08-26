from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from integrations.repositories.tables import (
    patrol_batch,
    patrol_delivery_outbox,
    patrol_job,
    patrol_operating_unit_catalog,
    patrol_raw_fact,
    patrol_run,
    patrol_signal,
    patrol_signal_occurrence,
)


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class RuntimeQueryService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(patrol_run).where(patrol_run.c.run_id == run_id)
            ).mappings().one_or_none()
            if row is None:
                return None
            outbox = connection.execute(
                sa.select(
                    patrol_delivery_outbox.c.outbox_id,
                    patrol_delivery_outbox.c.status,
                    patrol_delivery_outbox.c.retry_count,
                    patrol_delivery_outbox.c.last_error,
                    patrol_delivery_outbox.c.published_at,
                ).where(
                    patrol_delivery_outbox.c.aggregate_type == "INSPECTION_RUN",
                    patrol_delivery_outbox.c.aggregate_id == run_id,
                )
            ).mappings().one_or_none()
        return {
            "contract_version": "amazon_ops.inspection_run.v1",
            "run_id": row["run_id"],
            "job_id": row["job_id"],
            "batch_id": row["batch_id"],
            "request_id": row["request_id"],
            "operating_unit_id": row["operating_unit_id"],
            "trigger_type": row["trigger_type"],
            "status": row["status"],
            "rule_versions": dict(row["rule_versions_json"]),
            "fact_snapshot_id": row["fact_snapshot_id"],
            "signal_count": int(row["signal_count"]),
            "data_gap_count": int(row["data_gap_count"]),
            "started_at": _utc_aware(row["started_at"]),
            "finished_at": _utc_aware(row["finished_at"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "delivery": (
                {
                    "outbox_id": outbox["outbox_id"],
                    "status": outbox["status"],
                    "retry_count": int(outbox["retry_count"]),
                    "last_error": outbox["last_error"],
                    "published_at": _utc_aware(outbox["published_at"]),
                }
                if outbox is not None
                else None
            ),
        }

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(patrol_signal).where(patrol_signal.c.signal_id == signal_id)
            ).mappings().one_or_none()
            if row is None:
                return None
            occurrences = connection.execute(
                sa.select(patrol_signal_occurrence)
                .where(patrol_signal_occurrence.c.signal_id == signal_id)
                .order_by(
                    patrol_signal_occurrence.c.occurred_at,
                    patrol_signal_occurrence.c.occurrence_id,
                )
            ).mappings().all()
        signal = dict(row["signal_payload_json"])
        signal.update(
            signal_state=row["signal_state"],
            version=int(row["version"]),
            recurrence_count=int(row["recurrence_count"]),
            consecutive_miss_count=int(row["consecutive_miss_count"]),
            next_inspection_at=_utc_aware(row["next_inspection_at"]),
            valid_until=_utc_aware(row["valid_until"]),
            last_scan_run_id=row["last_scan_run_id"],
        )
        return {
            "signal": signal,
            "occurrences": [
                {
                    "occurrence_id": item["occurrence_id"],
                    "run_id": item["run_id"],
                    "snapshot_id": item["snapshot_id"],
                    "occurrence_type": item["occurrence_type"],
                    "severity": item["severity"],
                    "evidence_refs": list(item["evidence_refs_json"]),
                    "details": dict(item["details_json"]),
                    "occurred_at": _utc_aware(item["occurred_at"]),
                }
                for item in occurrences
            ],
        }

    def get_raw_facts(
        self,
        run_id: str,
        *,
        include_payload: bool = True,
    ) -> list[dict[str, Any]]:
        columns = [
            patrol_raw_fact.c.raw_fact_id,
            patrol_raw_fact.c.run_id,
            patrol_raw_fact.c.snapshot_id,
            patrol_raw_fact.c.operating_unit_id,
            patrol_raw_fact.c.as_of_date,
            patrol_raw_fact.c.fact_key,
            patrol_raw_fact.c.tool_name,
            patrol_raw_fact.c.request_hash,
            patrol_raw_fact.c.response_content_hash,
            patrol_raw_fact.c.status,
            patrol_raw_fact.c.is_core,
            patrol_raw_fact.c.row_count,
            patrol_raw_fact.c.latency_ms,
            patrol_raw_fact.c.warning,
            patrol_raw_fact.c.error_code,
            patrol_raw_fact.c.error_message,
            patrol_raw_fact.c.fetched_at,
            patrol_raw_fact.c.reused_from_raw_fact_id,
            patrol_raw_fact.c.created_at,
        ]
        if include_payload:
            columns.extend([
                patrol_raw_fact.c.request_json,
                patrol_raw_fact.c.response_json,
                patrol_raw_fact.c.extracted_data_json,
            ])
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(*columns)
                .where(patrol_raw_fact.c.run_id == run_id)
                .order_by(patrol_raw_fact.c.fact_key)
            ).mappings().all()
        facts = []
        for row in rows:
            item = dict(row)
            item["request_hash"] = f"sha256:{row['request_hash']}"
            if row["response_content_hash"]:
                item["response_content_hash"] = f"sha256:{row['response_content_hash']}"
            item["is_core"] = bool(row["is_core"])
            item["fetched_at"] = _utc_aware(row["fetched_at"])
            item["created_at"] = _utc_aware(row["created_at"])
            if include_payload:
                item["request_json"] = dict(row["request_json"])
                item["response_json"] = (
                    dict(row["response_json"]) if row["response_json"] is not None else None
                )
                item["extracted_data_json"] = (
                    list(row["extracted_data_json"])
                    if row["extracted_data_json"] is not None
                    else None
                )
            facts.append(item)
        return facts

    def get_catalog_owner_user_ids(self, operating_unit_id: str) -> list[int]:
        """查询 catalog 中已记录的负责人 user ID 列表。

        返回空列表表示该 operating_unit 还没有负责人记录。
        """
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json)
                .where(
                    patrol_operating_unit_catalog.c.operating_unit_id
                    == operating_unit_id
                )
            ).one_or_none()
        if row is None or not row[0]:
            return []
        owner_ids: list[int] = []
        for value in row[0]:
            try:
                owner_id = int(value)
            except (TypeError, ValueError):
                continue
            if owner_id > 0:
                owner_ids.append(owner_id)
        return sorted(set(owner_ids))

    def list_batches(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
    ) -> dict[str, Any]:
        conditions = []
        if status:
            conditions.append(patrol_batch.c.status == status)
        return self._page(
            table=patrol_batch,
            columns=(
                patrol_batch.c.batch_id,
                patrol_batch.c.trigger_type,
                patrol_batch.c.business_date,
                patrol_batch.c.status,
                patrol_batch.c.total_count,
                patrol_batch.c.pending_count,
                patrol_batch.c.running_count,
                patrol_batch.c.succeeded_count,
                patrol_batch.c.failed_count,
                patrol_batch.c.started_at,
                patrol_batch.c.finished_at,
            ),
            conditions=conditions,
            order_by=(patrol_batch.c.started_at.desc(), patrol_batch.c.batch_id.desc()),
            page=page,
            page_size=page_size,
        )

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        conditions = []
        if status:
            conditions.append(patrol_job.c.status == status)
        if batch_id:
            conditions.append(patrol_job.c.batch_id == batch_id)
        return self._page(
            table=patrol_job,
            columns=(
                patrol_job.c.job_id,
                patrol_job.c.batch_id,
                patrol_job.c.operating_unit_id,
                patrol_job.c.shop_id,
                patrol_job.c.site_code,
                patrol_job.c.parent_asin,
                patrol_job.c.parent_seller_sku,
                patrol_job.c.status,
                patrol_job.c.result_ref,
                patrol_job.c.retry_count,
                patrol_job.c.max_retry,
                patrol_job.c.last_error_code,
                patrol_job.c.last_error_message,
                patrol_job.c.created_at,
            ),
            conditions=conditions,
            order_by=(patrol_job.c.created_at.desc(), patrol_job.c.job_id.desc()),
            page=page,
            page_size=page_size,
        )

    def list_runs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        conditions = []
        if status:
            conditions.append(patrol_run.c.status == status)
        if batch_id:
            conditions.append(patrol_run.c.batch_id == batch_id.strip())
        return self._page(
            table=patrol_run,
            columns=(
                patrol_run.c.run_id,
                patrol_run.c.job_id,
                patrol_run.c.batch_id,
                patrol_run.c.operating_unit_id,
                patrol_run.c.trigger_type,
                patrol_run.c.status,
                patrol_run.c.signal_count,
                patrol_run.c.data_gap_count,
                patrol_run.c.started_at,
                patrol_run.c.finished_at,
                patrol_run.c.error_code,
                patrol_run.c.error_message,
            ),
            conditions=conditions,
            order_by=(patrol_run.c.started_at.desc(), patrol_run.c.run_id.desc()),
            page=page,
            page_size=page_size,
        )

    def list_signals(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        state: str | None = None,
        severity: str | None = None,
        signal_type: str | None = None,
        batch_id: str | None = None,
        parent_asin: str | None = None,
        latest_detection_only: bool = False,
    ) -> dict[str, Any]:
        conditions = []
        if state:
            conditions.append(patrol_signal.c.signal_state == state)
        if severity:
            conditions.append(patrol_signal.c.severity == severity)
        if signal_type:
            conditions.append(patrol_signal.c.signal_type == signal_type)
        latest_detection = sa.exists(
            sa.select(1).where(
                patrol_signal_occurrence.c.signal_id == patrol_signal.c.signal_id,
                patrol_signal_occurrence.c.run_id == patrol_signal.c.last_scan_run_id,
                patrol_signal_occurrence.c.occurred_at == patrol_signal.c.last_detected_at,
                patrol_signal_occurrence.c.occurrence_type.in_(
                    ("DETECTED", "DATA_GAP", "RECURRED")
                ),
            )
        )
        if latest_detection_only:
            conditions.append(latest_detection)
        if batch_id:
            batch_run = patrol_signal_occurrence.join(
                patrol_run,
                patrol_signal_occurrence.c.run_id == patrol_run.c.run_id,
            )
            conditions.append(
                sa.exists(
                    sa.select(1)
                    .select_from(batch_run)
                    .where(
                        patrol_signal_occurrence.c.signal_id == patrol_signal.c.signal_id,
                        patrol_signal_occurrence.c.run_id
                        == patrol_signal.c.last_scan_run_id,
                        patrol_signal_occurrence.c.occurred_at
                        == patrol_signal.c.last_detected_at,
                        patrol_signal_occurrence.c.occurrence_type.in_(
                            ("DETECTED", "DATA_GAP", "RECURRED")
                        ),
                        patrol_run.c.batch_id == batch_id.strip(),
                    )
                )
            )
        if parent_asin:
            conditions.append(
                sa.exists(
                    sa.select(1)
                    .select_from(patrol_job)
                    .where(
                        patrol_job.c.operating_unit_id
                        == patrol_signal.c.operating_unit_id,
                        patrol_job.c.parent_asin == parent_asin.strip().upper(),
                    )
                )
            )
        result = self._page(
            table=patrol_signal,
            columns=(
                patrol_signal.c.signal_id,
                patrol_signal.c.operating_unit_id,
                patrol_signal.c.issue_code,
                patrol_signal.c.signal_type,
                patrol_signal.c.severity,
                patrol_signal.c.signal_state,
                patrol_signal.c.action_timing_status,
                patrol_signal.c.execution_readiness,
                patrol_signal.c.first_detected_at,
                patrol_signal.c.last_detected_at,
                patrol_signal.c.last_scan_run_id,
                patrol_signal.c.recurrence_count,
                patrol_signal.c.next_inspection_at,
                patrol_signal.c.version,
                patrol_signal.c.signal_payload_json,
            ),
            conditions=conditions,
            order_by=(
                patrol_signal.c.last_detected_at.desc(),
                patrol_signal.c.signal_id.desc(),
            ),
            page=page,
            page_size=page_size,
        )
        for item in result["items"]:
            payload = dict(item.pop("signal_payload_json") or {})
            identity = payload.get("operating_unit_ref") or {}
            item["shop_id"] = identity.get("shop_id")
            item["site_code"] = identity.get("site_code")
            item["parent_asin"] = identity.get("parent_asin")
            child_scope = payload.get("child_scope") or {}
            item["child_asins"] = list(child_scope.get("child_asins") or [])
            item["child_skus"] = list(child_scope.get("child_skus") or [])
            diagnosis = payload.get("diagnosis") or {}
            item["diagnosis_summary"] = diagnosis.get("summary")
        return result

    def workspace_summary(self) -> dict[str, Any]:
        active_states = ("RESOLVED", "FALSE_POSITIVE", "IGNORED", "INTERRUPTED")
        with self.engine.connect() as connection:
            latest_batch = connection.execute(
                sa.select(
                    patrol_batch.c.batch_id,
                    patrol_batch.c.status,
                    patrol_batch.c.started_at,
                    patrol_batch.c.finished_at,
                )
                .order_by(patrol_batch.c.started_at.desc(), patrol_batch.c.batch_id.desc())
                .limit(1)
            ).mappings().one_or_none()

            def count(table: sa.Table, *conditions: Any) -> int:
                return int(connection.execute(
                    sa.select(sa.func.count()).select_from(table).where(*conditions)
                ).scalar_one())

            return {
                "batches": count(patrol_batch),
                "jobs": count(patrol_job),
                "runs": count(patrol_run),
                "signals": count(patrol_signal),
                "active_signals": count(
                    patrol_signal,
                    patrol_signal.c.signal_state.not_in(active_states),
                ),
                "due_signals": count(
                    patrol_signal,
                    patrol_signal.c.next_inspection_at.is_not(None),
                    patrol_signal.c.next_inspection_at <= datetime.now(UTC).replace(tzinfo=None),
                ),
                "latest_batch": (
                    {
                        **dict(latest_batch),
                        "started_at": _utc_aware(latest_batch["started_at"]),
                        "finished_at": _utc_aware(latest_batch["finished_at"]),
                    }
                    if latest_batch is not None
                    else None
                ),
            }

    def _page(
        self,
        *,
        table: sa.Table,
        columns: tuple[Any, ...],
        conditions: list[Any],
        order_by: tuple[Any, ...],
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        offset = (page - 1) * page_size
        with self.engine.connect() as connection:
            total = int(connection.execute(
                sa.select(sa.func.count()).select_from(table).where(*conditions)
            ).scalar_one())
            rows = connection.execute(
                sa.select(*columns)
                .where(*conditions)
                .order_by(*order_by)
                .offset(offset)
                .limit(page_size)
            ).mappings().all()
        items = []
        for row in rows:
            item = dict(row)
            for key, value in item.items():
                if isinstance(value, datetime):
                    item[key] = _utc_aware(value)
            items.append(item)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
        }
