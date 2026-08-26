from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from types import TracebackType
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError

from clients.mcp_client import McpToolResult, request_hash
from clients.mcp_security import sanitize_mcp_payload, sanitize_mcp_text
from core.contracts import (
    InspectionSignal,
    OperatingFactSnapshot,
    canonical_json,
    sha256_json,
)
from core.enums import DeliveryStatus, InspectionRunStatus, SignalState, SignalType
from core.inspection_run import TERMINAL_RUN_STATUSES, InspectionRun
from core.result_envelope import InspectionResultEnvelope
from integrations.repositories.tables import (
    patrol_delivery_outbox,
    patrol_fact_snapshot,
    patrol_operating_unit_catalog,
    patrol_raw_fact,
    patrol_run,
    patrol_signal,
    patrol_signal_occurrence,
)


class RepositoryInvariantError(ValueError):
    pass


class RepositoryConflict(RuntimeError):
    pass


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise RepositoryInvariantError("persistent datetimes must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _hash64(value: str) -> str:
    prefix = "sha256:"
    digest = value.removeprefix(prefix)
    if len(digest) != 64:
        raise RepositoryInvariantError("expected a SHA-256 digest")
    return digest


def _child_scope_key(signal: InspectionSignal) -> str:
    if signal.child_scope is None:
        return ""
    from hashlib import sha256

    payload = canonical_json(signal.child_scope.model_dump(mode="json"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _dedup_key(signal: InspectionSignal) -> str:
    from hashlib import sha256

    identity = "\x1f".join(
        [
            signal.operating_unit_ref.operating_unit_id,
            signal.issue_code,
            _child_scope_key(signal),
        ]
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def _snapshot_window(snapshot: OperatingFactSnapshot) -> tuple[datetime | None, datetime | None]:
    starts = [item.window_start for item in snapshot.source_refs if item.window_start]
    ends = [item.window_end for item in snapshot.source_refs if item.window_end]

    def at_midnight(value: date) -> datetime:
        return datetime.combine(value, time.min)

    return (
        at_midnight(min(starts)) if starts else None,
        at_midnight(max(ends)) if ends else None,
    )


class PatrolRepository:
    """SQLAlchemy Core repository bound to one caller-owned transaction."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def create_run(self, run: InspectionRun) -> None:
        if run.status in TERMINAL_RUN_STATUSES:
            raise RepositoryInvariantError("create_run expects a non-terminal run")
        try:
            self.connection.execute(
                sa.insert(patrol_run).values(
                    run_id=run.run_id,
                    job_id=run.job_id,
                    batch_id=run.batch_id,
                    request_id=run.request_id,
                    operating_unit_id=run.operating_unit_id,
                    trigger_type=run.trigger_type.value,
                    status=run.status.value,
                    rule_versions_json=run.rule_versions,
                    fact_snapshot_id=None,
                    signal_count=0,
                    data_gap_count=0,
                    started_at=_utc_naive(run.started_at),
                    finished_at=None,
                    error_code=None,
                    error_message=None,
                )
            )
        except IntegrityError as exc:
            raise RepositoryConflict("run or request_id already exists") from exc

    def get_run(self, run_id: str) -> InspectionRun | None:
        row = self.connection.execute(
            sa.select(patrol_run).where(patrol_run.c.run_id == run_id)
        ).mappings().one_or_none()
        if row is None:
            return None

        def utc_aware(value: datetime | None) -> datetime | None:
            return value.replace(tzinfo=UTC) if value is not None else None

        return InspectionRun(
            run_id=row["run_id"],
            job_id=row["job_id"],
            batch_id=row["batch_id"],
            request_id=row["request_id"],
            operating_unit_id=row["operating_unit_id"],
            trigger_type=row["trigger_type"],
            status=row["status"],
            rule_versions=row["rule_versions_json"],
            fact_snapshot_id=row["fact_snapshot_id"],
            signal_count=row["signal_count"],
            data_gap_count=row["data_gap_count"],
            started_at=utc_aware(row["started_at"]),
            finished_at=utc_aware(row["finished_at"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def fail_run(self, run: InspectionRun) -> None:
        if run.status is not InspectionRunStatus.FAILED:
            raise RepositoryInvariantError("fail_run requires FAILED status")
        updated = self.connection.execute(
            sa.update(patrol_run)
            .where(
                patrol_run.c.run_id == run.run_id,
                patrol_run.c.status.not_in(
                    [status.value for status in TERMINAL_RUN_STATUSES]
                ),
            )
            .values(
                status=run.status.value,
                finished_at=_utc_naive(run.finished_at),
                error_code=run.error_code,
                error_message=run.error_message,
            )
        )
        if updated.rowcount != 1:
            raise RepositoryConflict("run is missing or already terminal")

    def persist_raw_facts(
        self,
        *,
        run_id: str,
        operating_unit_id: str,
        as_of_date: date,
        calls: dict[str, tuple[str, dict]],
        results: dict[str, McpToolResult | Exception],
        core_keys: frozenset[str],
        fetched_at: datetime,
        reused_from: dict[str, str] | None = None,
    ) -> None:
        run_unit = self.connection.execute(
            sa.select(patrol_run.c.operating_unit_id).where(patrol_run.c.run_id == run_id)
        ).scalar_one_or_none()
        if run_unit is None:
            raise RepositoryInvariantError("run must exist before archiving raw facts")
        if run_unit != operating_unit_id:
            raise RepositoryInvariantError("raw facts do not match the run operating unit")
        if set(calls) != set(results):
            raise RepositoryInvariantError("raw fact calls and results must have identical keys")

        rows = []
        for fact_key, (tool_name, arguments) in calls.items():
            result = results[fact_key]
            safe_arguments = sanitize_mcp_payload(arguments)
            if safe_arguments != arguments:
                raise RepositoryInvariantError(
                    f"MCP request arguments contain forbidden credential fields: {fact_key}"
                )
            common = {
                "raw_fact_id": f"rf_{uuid4().hex[:24]}",
                "run_id": run_id,
                "snapshot_id": None,
                "operating_unit_id": operating_unit_id,
                "as_of_date": as_of_date,
                "fact_key": fact_key,
                "tool_name": tool_name,
                "request_hash": _hash64(request_hash(tool_name, arguments)),
                "request_json": safe_arguments,
                "is_core": fact_key in core_keys,
                "fetched_at": _utc_naive(
                    result.fetched_at
                    if isinstance(result, McpToolResult) and result.fetched_at
                    else fetched_at
                ),
                "reused_from_raw_fact_id": (reused_from or {}).get(fact_key),
            }
            if isinstance(result, McpToolResult):
                safe_data = sanitize_mcp_payload(result.data)
                safe_raw = sanitize_mcp_payload(result.raw)
                rows.append({
                    **common,
                    "response_content_hash": _hash64(sha256_json(safe_data)),
                    "response_json": safe_raw,
                    "extracted_data_json": safe_data,
                    "status": "SUCCESS",
                    "row_count": len(result.data),
                    "latency_ms": result.latency_ms,
                    "warning": (
                        sanitize_mcp_text(result.warning)
                        if result.warning is not None
                        else None
                    ),
                    "error_code": None,
                    "error_message": None,
                })
            else:
                rows.append({
                    **common,
                    "response_content_hash": None,
                    "response_json": None,
                    "extracted_data_json": None,
                    "status": "FAILED",
                    "row_count": None,
                    "latency_ms": None,
                    "warning": None,
                    "error_code": str(
                        getattr(result, "code", type(result).__name__)
                    ).upper()[:64],
                    "error_message": sanitize_mcp_text(str(result))[:65535],
                })
        try:
            self.connection.execute(sa.insert(patrol_raw_fact), rows)
        except IntegrityError as exc:
            raise RepositoryConflict("raw facts already exist for this run") from exc

    def load_reusable_raw_facts(
        self,
        *,
        source_run_id: str,
        operating_unit_id: str,
        expected_fact_keys: frozenset[str],
    ) -> tuple[
        date,
        datetime,
        dict[str, tuple[str, dict]],
        dict[str, McpToolResult],
        dict[str, str],
    ]:
        rows = self.connection.execute(
            sa.select(patrol_raw_fact)
            .where(patrol_raw_fact.c.run_id == source_run_id)
            .order_by(patrol_raw_fact.c.fact_key)
        ).mappings().all()
        if not rows:
            raise RepositoryInvariantError("source run has no archived raw facts")
        if any(row["operating_unit_id"] != operating_unit_id for row in rows):
            raise RepositoryInvariantError("source run belongs to another operating unit")
        actual_keys = {row["fact_key"] for row in rows}
        if not expected_fact_keys.issubset(actual_keys):
            raise RepositoryInvariantError("source run does not contain the complete fact set")
        if any(row["status"] != "SUCCESS" and row["is_core"] for row in rows):
            raise RepositoryInvariantError("source run contains failed core facts and cannot be reused")
        as_of_dates = {row["as_of_date"] for row in rows}
        if len(as_of_dates) != 1:
            raise RepositoryInvariantError("source raw facts have inconsistent as-of dates")
        fetched_at = min(row["fetched_at"] for row in rows).replace(tzinfo=UTC)
        calls: dict[str, tuple[str, dict]] = {}
        results: dict[str, McpToolResult] = {}
        source_ids: dict[str, str] = {}
        for row in rows:
            fact_key = row["fact_key"]
            calls[fact_key] = (row["tool_name"], dict(row["request_json"]))
            if row["status"] == "SUCCESS":
                results[fact_key] = McpToolResult(
                    tool_name=row["tool_name"],
                    data=list(row["extracted_data_json"]),
                    raw=dict(row["response_json"]),
                    request_hash=f"sha256:{row['request_hash']}",
                    latency_ms=int(row["latency_ms"] or 0),
                    warning=row["warning"],
                    fetched_at=row["fetched_at"].replace(tzinfo=UTC),
                )
                if _hash64(results[fact_key].content_hash) != row["response_content_hash"]:
                    raise RepositoryInvariantError("archived raw fact content hash mismatch")
            else:
                results[fact_key] = RuntimeError(
                    row["error_message"] or row["error_code"] or "archived fact failed"
                )
            if _hash64(request_hash(row["tool_name"], calls[fact_key][1])) != row["request_hash"]:
                raise RepositoryInvariantError("archived raw fact request hash mismatch")
            source_ids[fact_key] = row["raw_fact_id"]
        return as_of_dates.pop(), fetched_at, calls, results, source_ids

    def list_signals_for_update(self, operating_unit_id: str):
        from inspector.signal_reconciler import StoredSignal

        rows = self.connection.execute(
            sa.select(
                patrol_signal.c.signal_id,
                patrol_signal.c.issue_code,
                patrol_signal.c.child_scope_key,
                patrol_signal.c.signal_state,
                patrol_signal.c.first_detected_at,
                patrol_signal.c.last_detected_at,
                patrol_signal.c.recurrence_count,
                patrol_signal.c.consecutive_miss_count,
                patrol_signal.c.version,
                patrol_signal.c.signal_payload_json,
            )
            .where(patrol_signal.c.operating_unit_id == operating_unit_id)
            .with_for_update()
        ).mappings()
        stored_signals = []
        for row in rows:
            payload = row["signal_payload_json"]
            if not isinstance(payload, dict) or "contract_version" not in payload:
                raise RepositoryInvariantError(
                    f"signal payload requires migration backfill: {row['signal_id']}"
                )
            stored_signals.append(StoredSignal(
                signal_id=row["signal_id"],
                issue_code=row["issue_code"],
                child_scope_key=row["child_scope_key"],
                signal_state=SignalState(row["signal_state"]),
                first_detected_at=row["first_detected_at"].replace(tzinfo=UTC),
                last_detected_at=row["last_detected_at"].replace(tzinfo=UTC),
                recurrence_count=row["recurrence_count"],
                consecutive_miss_count=row["consecutive_miss_count"],
                version=row["version"],
                payload=InspectionSignal.model_validate(payload),
            ))
        return stored_signals

    def apply_reconciliation(
        self,
        *,
        result,
        run_id: str,
        snapshot_id: str,
    ) -> None:
        from inspector.signal_reconciler import SignalMutationKind

        for mutation in result.mutations:
            if mutation.kind is SignalMutationKind.INSERT:
                if mutation.candidate is None:
                    raise RepositoryInvariantError("insert mutation requires candidate")
                self._insert_signal(mutation.candidate)
            else:
                values = {
                    "signal_state": mutation.signal_state.value,
                    "recurrence_count": mutation.recurrence_count,
                    "consecutive_miss_count": mutation.consecutive_miss_count,
                    "last_scan_run_id": run_id,
                    "next_inspection_at": None,
                    "version": mutation.version,
                }
                if mutation.candidate is not None:
                    candidate = mutation.candidate
                    exposure = candidate.economic_exposure
                    values.update(
                        signal_type=candidate.signal_type.value,
                        severity=candidate.severity.value,
                        action_timing_status=candidate.action_timing_status.value,
                        economic_currency=exposure.currency,
                        economic_amount=exposure.amount,
                        economic_window=exposure.window.value,
                        constraint_flags_json=[
                            item.value for item in candidate.constraint_flags
                        ],
                        execution_readiness=candidate.execution_readiness.value,
                        diagnosis_json=candidate.diagnosis.model_dump(mode="json"),
                        handoff_json=candidate.handoff_directive.model_dump(mode="json"),
                        signal_payload_json=candidate.model_dump(mode="json"),
                        last_detected_at=_utc_naive(candidate.last_detected_at),
                        valid_until=_utc_naive(candidate.valid_until),
                    )
                updated = self.connection.execute(
                    sa.update(patrol_signal)
                    .where(
                        patrol_signal.c.signal_id == mutation.signal_id,
                        patrol_signal.c.version == mutation.version - 1,
                    )
                    .values(**values)
                )
                if updated.rowcount != 1:
                    raise RepositoryConflict(
                        f"signal version conflict: {mutation.signal_id}"
                    )
            self.connection.execute(
                sa.insert(patrol_signal_occurrence).values(
                    occurrence_id=f"oc_{uuid4().hex[:24]}",
                    signal_id=mutation.signal_id,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    occurrence_type=mutation.occurrence_type,
                    severity=(
                        mutation.candidate.severity.value
                        if mutation.candidate is not None
                        else None
                    ),
                    evidence_refs_json=(
                        mutation.candidate.evidence_refs
                        if mutation.candidate is not None
                        else [snapshot_id]
                    ),
                    details_json={
                        "signal_state": mutation.signal_state.value,
                        "consecutive_miss_count": mutation.consecutive_miss_count,
                        "recurrence_count": mutation.recurrence_count,
                        "signal_version": mutation.version,
                    },
                    occurred_at=_utc_naive(mutation.occurred_at),
                )
            )

    def persist_result(
        self,
        envelope: InspectionResultEnvelope,
        snapshot: OperatingFactSnapshot,
        *,
        delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
    ) -> str:
        run = envelope.run
        if run.status not in TERMINAL_RUN_STATUSES:
            raise RepositoryInvariantError("persist_result requires a terminal run")
        if run.fact_snapshot_id != snapshot.snapshot_id:
            raise RepositoryInvariantError("run and snapshot references do not match")
        if run.operating_unit_id != snapshot.operating_unit.operating_unit_id:
            raise RepositoryInvariantError("run and snapshot operating units do not match")

        current = self.connection.execute(
            sa.select(
                patrol_run.c.request_id,
                patrol_run.c.job_id,
                patrol_run.c.batch_id,
                patrol_run.c.operating_unit_id,
                patrol_run.c.status,
            )
            .where(patrol_run.c.run_id == run.run_id)
            .with_for_update()
        ).mappings().one_or_none()
        if current is None:
            raise RepositoryInvariantError("run must be created before persisting its result")
        for field in ("request_id", "job_id", "batch_id", "operating_unit_id"):
            if current[field] != getattr(run, field):
                raise RepositoryConflict(f"run {field} changed after creation")
        if current["status"] in {status.value for status in TERMINAL_RUN_STATUSES}:
            raise RepositoryConflict("run result is already terminal")

        self._insert_snapshot(run.run_id, snapshot)
        self._backfill_catalog_owner(snapshot)
        self._link_raw_facts(run.run_id, snapshot.snapshot_id)
        for signal in envelope.signals:
            self._insert_signal(signal)
            self._insert_occurrence(signal, snapshot.snapshot_id)

        outbox_id = f"ob_{uuid4().hex[:24]}"
        self.connection.execute(
            sa.insert(patrol_delivery_outbox).values(
                outbox_id=outbox_id,
                aggregate_type="INSPECTION_RUN",
                aggregate_id=run.run_id,
                aggregate_version=1,
                payload_hash=envelope.content_hash(),
                payload_json=envelope.model_dump(mode="json"),
                status=delivery_status.value,
                retry_count=0,
                max_retry=10,
                next_retry_at=None,
            )
        )
        updated = self.connection.execute(
            sa.update(patrol_run)
            .where(patrol_run.c.run_id == run.run_id)
            .values(
                status=run.status.value,
                rule_versions_json=run.rule_versions,
                fact_snapshot_id=run.fact_snapshot_id,
                signal_count=run.signal_count,
                data_gap_count=run.data_gap_count,
                finished_at=_utc_naive(run.finished_at),
                error_code=run.error_code,
                error_message=run.error_message,
            )
        )
        if updated.rowcount != 1:
            raise RepositoryConflict("run disappeared while persisting result")
        return outbox_id

    def persist_reconciled_result(
        self,
        *,
        envelope: InspectionResultEnvelope,
        snapshot: OperatingFactSnapshot,
        reconciliation,
        delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
    ) -> str:
        run = envelope.run
        if run.status not in TERMINAL_RUN_STATUSES:
            raise RepositoryInvariantError("terminal run is required")
        if run.fact_snapshot_id != snapshot.snapshot_id:
            raise RepositoryInvariantError("run and snapshot references do not match")
        current = self.connection.execute(
            sa.select(
                patrol_run.c.request_id,
                patrol_run.c.job_id,
                patrol_run.c.batch_id,
                patrol_run.c.operating_unit_id,
                patrol_run.c.status,
            )
            .where(patrol_run.c.run_id == run.run_id)
            .with_for_update()
        ).mappings().one_or_none()
        if current is None:
            raise RepositoryInvariantError("run must be created before finalization")
        for field in ("request_id", "job_id", "batch_id", "operating_unit_id"):
            if current[field] != getattr(run, field):
                raise RepositoryConflict(f"run {field} changed after creation")
        if current["status"] in {status.value for status in TERMINAL_RUN_STATUSES}:
            raise RepositoryConflict("run result is already terminal")

        self._insert_snapshot(run.run_id, snapshot)
        self._backfill_catalog_owner(snapshot)
        self._link_raw_facts(run.run_id, snapshot.snapshot_id)
        self.apply_reconciliation(
            result=reconciliation,
            run_id=run.run_id,
            snapshot_id=snapshot.snapshot_id,
        )
        outbox_id = f"ob_{uuid4().hex[:24]}"
        self.connection.execute(
            sa.insert(patrol_delivery_outbox).values(
                outbox_id=outbox_id,
                aggregate_type="INSPECTION_RUN",
                aggregate_id=run.run_id,
                aggregate_version=1,
                payload_hash=envelope.content_hash(),
                payload_json=envelope.model_dump(mode="json"),
                status=delivery_status.value,
                retry_count=0,
                max_retry=10,
                next_retry_at=None,
            )
        )
        updated = self.connection.execute(
            sa.update(patrol_run)
            .where(patrol_run.c.run_id == run.run_id)
            .values(
                status=run.status.value,
                rule_versions_json=run.rule_versions,
                fact_snapshot_id=run.fact_snapshot_id,
                signal_count=run.signal_count,
                data_gap_count=run.data_gap_count,
                finished_at=_utc_naive(run.finished_at),
                error_code=run.error_code,
                error_message=run.error_message,
            )
        )
        if updated.rowcount != 1:
            raise RepositoryConflict("run disappeared while finalizing result")
        return outbox_id

    def _insert_snapshot(self, run_id: str, snapshot: OperatingFactSnapshot) -> None:
        window_start, window_end = _snapshot_window(snapshot)
        snapshot_json = snapshot.model_dump(mode="json")
        normalized = {
            field: snapshot_json[field]
            for field in (
                "identity",
                "sales",
                "traffic",
                "profit",
                "inventory",
                "price",
                "quality",
                "execution_history",
            )
        }
        raw_fact_ids = {
            row["tool_name"]: row["raw_fact_id"]
            for row in self.connection.execute(
                sa.select(patrol_raw_fact.c.tool_name, patrol_raw_fact.c.raw_fact_id).where(
                    patrol_raw_fact.c.run_id == run_id,
                    patrol_raw_fact.c.status == "SUCCESS",
                )
            ).mappings()
        }
        raw_refs = [
            {
                "tool_name": item.tool_name,
                "request_hash": item.request_hash,
                "content_hash": item.content_hash,
                "raw_fact_id": raw_fact_ids.get(item.tool_name),
            }
            for item in snapshot.source_refs
        ]
        self.connection.execute(
            sa.insert(patrol_fact_snapshot).values(
                snapshot_id=snapshot.snapshot_id,
                run_id=run_id,
                operating_unit_id=snapshot.operating_unit.operating_unit_id,
                as_of=snapshot.as_of_time.date(),
                window_start=window_start,
                window_end=window_end,
                content_hash=_hash64(snapshot.content_hash),
                quality_status=snapshot.quality_status.value,
                completeness_score=(
                    Decimal(str(snapshot.completeness_score)) * Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                source_refs_json=snapshot_json["source_refs"],
                data_gaps_json=snapshot_json["data_gaps"],
                normalized_summary_json=normalized,
                raw_reference_json=raw_refs or None,
            )
        )

    def _backfill_catalog_owner(self, snapshot: OperatingFactSnapshot) -> None:
        identity = snapshot.identity or {}
        if identity.get("owner_source") != "product_info":
            return
        raw_owner_ids = identity.get("owner_user_ids") or []
        owner_ids: set[int] = set()
        for value in raw_owner_ids:
            try:
                owner_id = int(value)
            except (TypeError, ValueError):
                continue
            if owner_id > 0:
                owner_ids.add(owner_id)
        if not owner_ids:
            return
        row = self.connection.execute(
            sa.select(patrol_operating_unit_catalog.c.owner_user_ids_json)
            .where(
                patrol_operating_unit_catalog.c.operating_unit_id
                == snapshot.operating_unit.operating_unit_id
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            return
        existing_ids: set[int] = set()
        for value in (row[0] or []):
            try:
                owner_id = int(value)
            except (TypeError, ValueError):
                continue
            if owner_id > 0:
                existing_ids.add(owner_id)
        merged = sorted(existing_ids | owner_ids)
        if merged == sorted(existing_ids):
            return
        self.connection.execute(
            sa.update(patrol_operating_unit_catalog)
            .where(
                patrol_operating_unit_catalog.c.operating_unit_id
                == snapshot.operating_unit.operating_unit_id
            )
            .values(owner_user_ids_json=merged, updated_at=_utc_naive(datetime.now(UTC)))
        )

    def _link_raw_facts(self, run_id: str, snapshot_id: str) -> None:
        self.connection.execute(
            sa.update(patrol_raw_fact)
            .where(patrol_raw_fact.c.run_id == run_id)
            .values(snapshot_id=snapshot_id)
        )

    def _insert_signal(self, signal: InspectionSignal) -> None:
        exposure = signal.economic_exposure
        self.connection.execute(
            sa.insert(patrol_signal).values(
                signal_id=signal.signal_id,
                dedup_key=_dedup_key(signal),
                operating_unit_id=signal.operating_unit_ref.operating_unit_id,
                issue_code=signal.issue_code,
                child_scope_key=_child_scope_key(signal),
                signal_type=signal.signal_type.value,
                severity=signal.severity.value,
                signal_state=signal.signal_state.value,
                action_timing_status=signal.action_timing_status.value,
                economic_currency=exposure.currency,
                economic_amount=exposure.amount,
                economic_window=exposure.window.value,
                constraint_flags_json=[item.value for item in signal.constraint_flags],
                execution_readiness=signal.execution_readiness.value,
                diagnosis_json=signal.diagnosis.model_dump(mode="json"),
                handoff_json=signal.handoff_directive.model_dump(mode="json"),
                signal_payload_json=signal.model_dump(mode="json"),
                first_detected_at=_utc_naive(signal.first_detected_at),
                last_detected_at=_utc_naive(signal.last_detected_at),
                last_scan_run_id=signal.last_scan_run_id,
                recurrence_count=signal.recurrence_count,
                consecutive_miss_count=0,
                next_inspection_at=None,
                valid_until=_utc_naive(signal.valid_until),
                version=1,
            )
        )

    def _insert_occurrence(self, signal: InspectionSignal, snapshot_id: str) -> None:
        occurrence_type = (
            "DATA_GAP" if signal.signal_type is SignalType.DATA_QUALITY else "DETECTED"
        )
        self.connection.execute(
            sa.insert(patrol_signal_occurrence).values(
                occurrence_id=f"oc_{uuid4().hex[:24]}",
                signal_id=signal.signal_id,
                run_id=signal.last_scan_run_id,
                snapshot_id=snapshot_id,
                occurrence_type=occurrence_type,
                severity=signal.severity.value,
                evidence_refs_json=signal.evidence_refs,
                details_json={
                    "issue_code": signal.issue_code,
                    "signal_state": signal.signal_state.value,
                },
                occurred_at=_utc_naive(signal.last_detected_at),
            )
        )


class PatrolUnitOfWork:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.connection: Connection | None = None
        self.repository: PatrolRepository | None = None
        self._transaction = None

    def __enter__(self) -> PatrolUnitOfWork:
        self.connection = self.engine.connect()
        self._transaction = self.connection.begin()
        self.repository = PatrolRepository(self.connection)
        return self

    def commit(self) -> None:
        if self._transaction is None:
            raise RuntimeError("unit of work is not active")
        self._transaction.commit()

    def rollback(self) -> None:
        if self._transaction is not None and self._transaction.is_active:
            self._transaction.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self.rollback()
            elif self._transaction is not None and self._transaction.is_active:
                self.rollback()
        finally:
            if self.connection is not None:
                self.connection.close()
