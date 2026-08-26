from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from core.enums import DeliveryStatus, FeedbackInboxStatus, JobStatus
from integrations.repositories.patrol import _utc_naive
from integrations.repositories.tables import (
    patrol_batch,
    patrol_delivery_outbox,
    patrol_feedback_inbox,
    patrol_job,
    patrol_run,
    patrol_scheduler_lock,
    patrol_signal,
)


class HealthLevel(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    pending_jobs_warn: int = 100
    oldest_pending_job_warn_minutes: int = 30
    stale_running_job_minutes: int = 30
    pending_outbox_warn: int = 100
    oldest_pending_outbox_warn_minutes: int = 15


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return max(0, int((now - aware).total_seconds()))


def evaluate_health(
    metrics: dict[str, Any],
    thresholds: HealthThresholds,
) -> tuple[HealthLevel, list[dict[str, str]]]:
    alerts: list[dict[str, str]] = []

    def alert(code: str, level: HealthLevel, message: str) -> None:
        alerts.append({"code": code, "level": level.value, "message": message})

    jobs = metrics["jobs"]
    outbox = metrics["outbox"]
    feedback = metrics["feedback"]
    runs = metrics["runs"]
    if jobs.get(JobStatus.DEAD.value, 0):
        alert("DEAD_JOBS", HealthLevel.DEGRADED, "job queue contains DEAD records")
    if outbox.get(DeliveryStatus.DEAD.value, 0):
        alert("DEAD_OUTBOX", HealthLevel.CRITICAL, "delivery outbox contains DEAD records")
    if metrics["stale_running_jobs"]:
        alert(
            "STALE_RUNNING_JOBS",
            HealthLevel.DEGRADED,
            "running jobs exceeded their lease (self-heals via reclaim_stale)",
        )
    if jobs.get(JobStatus.PENDING.value, 0) >= thresholds.pending_jobs_warn:
        alert("JOB_BACKLOG", HealthLevel.DEGRADED, "pending job count reached warning threshold")
    if (
        metrics["oldest_pending_job_age_seconds"] is not None
        and metrics["oldest_pending_job_age_seconds"]
        >= thresholds.oldest_pending_job_warn_minutes * 60
    ):
        alert("JOB_WAIT_TOO_LONG", HealthLevel.DEGRADED, "oldest pending job is delayed")
    if outbox.get(DeliveryStatus.PENDING.value, 0) >= thresholds.pending_outbox_warn:
        alert("OUTBOX_BACKLOG", HealthLevel.DEGRADED, "pending outbox count reached warning threshold")
    if (
        metrics["oldest_pending_outbox_age_seconds"] is not None
        and metrics["oldest_pending_outbox_age_seconds"]
        >= thresholds.oldest_pending_outbox_warn_minutes * 60
    ):
        alert("OUTBOX_WAIT_TOO_LONG", HealthLevel.DEGRADED, "oldest pending result is delayed")
    if feedback.get(FeedbackInboxStatus.REJECTED.value, 0):
        alert("FEEDBACK_REJECTED", HealthLevel.DEGRADED, "feedback was rejected in the last 24 hours")
    if runs.get("FAILED", 0):
        alert("RUN_FAILED", HealthLevel.DEGRADED, "inspection runs failed in the last 24 hours")
    levels = {item["level"] for item in alerts}
    if HealthLevel.CRITICAL.value in levels:
        return HealthLevel.CRITICAL, alerts
    if HealthLevel.DEGRADED.value in levels:
        return HealthLevel.DEGRADED, alerts
    return HealthLevel.HEALTHY, alerts


class RuntimeHealthService:
    def __init__(
        self,
        engine: Engine,
        *,
        feature_flags: dict[str, bool],
        thresholds: HealthThresholds | None = None,
    ) -> None:
        self.engine = engine
        self.feature_flags = feature_flags
        self.thresholds = thresholds or HealthThresholds()

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        try:
            with self.engine.connect() as connection:
                connection.execute(sa.text("SELECT 1")).scalar_one()
                metrics = self._metrics(connection, current)
        except Exception as exc:
            return {
                "status": HealthLevel.CRITICAL.value,
                "checked_at": current.isoformat(),
                "database": {"status": "UNAVAILABLE", "error": type(exc).__name__},
                "feature_flags": self.feature_flags,
                "alerts": [{
                    "code": "DATABASE_UNAVAILABLE",
                    "level": HealthLevel.CRITICAL.value,
                    "message": "patrol runtime database is unavailable",
                }],
            }
        status, alerts = evaluate_health(metrics, self.thresholds)
        return {
            "status": status.value,
            "checked_at": current.isoformat(),
            "database": {"status": "AVAILABLE"},
            "feature_flags": self.feature_flags,
            "metrics": metrics,
            "alerts": alerts,
        }

    def _metrics(self, connection, now: datetime) -> dict[str, Any]:
        since = _utc_naive(now - timedelta(hours=24))
        stale_before = _utc_naive(
            now - timedelta(minutes=self.thresholds.stale_running_job_minutes)
        )

        def grouped(table, column, *conditions) -> dict[str, int]:
            rows = connection.execute(
                sa.select(column, sa.func.count()).where(*conditions).group_by(column)
            )
            return {str(status): int(count) for status, count in rows}

        oldest_job = connection.execute(
            sa.select(sa.func.min(patrol_job.c.created_at)).where(
                patrol_job.c.status == JobStatus.PENDING.value
            )
        ).scalar_one()
        oldest_outbox = connection.execute(
            sa.select(sa.func.min(patrol_delivery_outbox.c.created_at)).where(
                patrol_delivery_outbox.c.status == DeliveryStatus.PENDING.value
            )
        ).scalar_one()
        active_runs = {
            str(status): int(count)
            for status, count in connection.execute(
                sa.select(patrol_run.c.status, sa.func.count())
                .select_from(
                    patrol_run.join(
                        patrol_job,
                        patrol_job.c.job_id == patrol_run.c.job_id,
                    )
                )
                .where(
                    patrol_run.c.started_at >= since,
                    patrol_job.c.status != JobStatus.ARCHIVED.value,
                    sa.or_(
                        patrol_run.c.status != "FAILED",
                        patrol_job.c.status != JobStatus.SUCCEEDED.value,
                    ),
                )
                .group_by(patrol_run.c.status)
            )
        }
        return {
            "batches": grouped(patrol_batch, patrol_batch.c.status),
            "jobs": grouped(patrol_job, patrol_job.c.status),
            "runs": active_runs,
            "outbox": grouped(patrol_delivery_outbox, patrol_delivery_outbox.c.status),
            "feedback": grouped(
                patrol_feedback_inbox,
                patrol_feedback_inbox.c.status,
                patrol_feedback_inbox.c.received_at >= since,
            ),
            "active_signals": int(connection.execute(
                sa.select(sa.func.count()).select_from(patrol_signal).where(
                    patrol_signal.c.signal_state.not_in(
                        ["RESOLVED", "FALSE_POSITIVE", "IGNORED", "INTERRUPTED"]
                    )
                )
            ).scalar_one()),
            "due_signals": int(connection.execute(
                sa.select(sa.func.count()).select_from(patrol_signal).where(
                    patrol_signal.c.next_inspection_at.is_not(None),
                    patrol_signal.c.next_inspection_at <= _utc_naive(now),
                )
            ).scalar_one()),
            "stale_running_jobs": int(connection.execute(
                sa.select(sa.func.count()).select_from(patrol_job).where(
                    patrol_job.c.status == JobStatus.RUNNING.value,
                    patrol_job.c.locked_at < stale_before,
                )
            ).scalar_one()),
            "oldest_pending_job_age_seconds": _age_seconds(oldest_job, now),
            "oldest_pending_outbox_age_seconds": _age_seconds(oldest_outbox, now),
            "scheduler_leases": int(connection.execute(
                sa.select(sa.func.count()).select_from(patrol_scheduler_lock).where(
                    patrol_scheduler_lock.c.expires_at > _utc_naive(now)
                )
            ).scalar_one()),
        }


def prometheus_metrics(snapshot: dict[str, Any]) -> str:
    status_value = {
        HealthLevel.HEALTHY.value: 0,
        HealthLevel.DEGRADED.value: 1,
        HealthLevel.CRITICAL.value: 2,
    }.get(snapshot.get("status"), 2)
    lines = [
        "# HELP patrol_health_status Runtime health: 0 healthy, 1 degraded, 2 critical.",
        "# TYPE patrol_health_status gauge",
        f"patrol_health_status {status_value}",
    ]
    metrics = snapshot.get("metrics") or {}
    for section in ("batches", "jobs", "runs", "outbox", "feedback"):
        values = metrics.get(section) or {}
        name = f"patrol_{section}_total"
        lines.extend((f"# TYPE {name} gauge",))
        for state, count in sorted(values.items()):
            lines.append(f'{name}{{status="{state}"}} {int(count)}')
    for key in (
        "active_signals",
        "due_signals",
        "stale_running_jobs",
        "oldest_pending_job_age_seconds",
        "oldest_pending_outbox_age_seconds",
        "scheduler_leases",
    ):
        value = metrics.get(key)
        if value is not None:
            lines.append(f"patrol_{key} {int(value)}")
    return "\n".join(lines) + "\n"
