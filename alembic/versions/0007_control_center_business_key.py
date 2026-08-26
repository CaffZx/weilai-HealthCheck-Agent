"""Replace the operating-unit identity with the control-center business key.

Revision ID: 0007_control_center_business_key
Revises: 0006_owner_directory
Create Date: 2026-08-05
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0007_control_center_business_key"
down_revision: str | None = "0006_owner_directory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTRACT_VERSION = "amazon_ops.v2"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
ID_TABLES = (
    "t_patrol_run",
    "t_patrol_fact_snapshot",
    "t_patrol_raw_fact",
    "t_patrol_signal",
    "t_patrol_review",
    "t_patrol_asin_owner",
    "t_patrol_job",
)
JSON_COLUMNS = {
    "t_patrol_batch": ("scope_json",),
    "t_patrol_job": ("payload_json",),
    "t_patrol_fact_snapshot": (
        "source_refs_json",
        "data_gaps_json",
        "normalized_summary_json",
        "raw_reference_json",
    ),
    "t_patrol_raw_fact": ("request_json", "response_json", "extracted_data_json"),
    "t_patrol_signal": (
        "diagnosis_json",
        "handoff_json",
        "signal_payload_json",
    ),
    "t_patrol_signal_occurrence": ("evidence_refs_json", "details_json"),
    "t_patrol_delivery_outbox": ("payload_json", "delivery_ack_json"),
    "t_patrol_feedback_inbox": ("payload_json", "result_json"),
    "t_patrol_review": ("request_json", "current_snapshot_json", "result_json"),
    "t_patrol_idempotency": ("response_json",),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _new_id(shop_id: int, parent_asin: str, parent_seller_sku: str) -> str:
    raw = f"{CONTRACT_VERSION}|{shop_id}|{parent_asin}|{parent_seller_sku}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"ou_{digest}"


def _replace_ids(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_ids(child, mapping) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_ids(child, mapping) for child in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def _recompute_embedded_snapshot_hashes(value: Any) -> Any:
    if isinstance(value, list):
        return [_recompute_embedded_snapshot_hashes(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _recompute_embedded_snapshot_hashes(child)
        for key, child in value.items()
    }
    unit = result.get("operating_unit")
    if not isinstance(unit, dict) or not str(result.get("snapshot_id") or "").startswith("fs_"):
        return result
    domains = (
        "identity",
        "sales",
        "traffic",
        "profit",
        "inventory",
        "price",
        "quality",
        "execution_history",
    )
    source_refs = result.get("source_refs") or []
    if result.get("quality_status") == "FAILED":
        body = {"operating_unit": unit, "domains": {}, "blocked": True}
    else:
        body = {
            "operating_unit": unit,
            "domains": {domain: result.get(domain, {}) for domain in domains},
            "source_content_hashes": sorted(
                item["content_hash"]
                for item in source_refs
                if isinstance(item, dict) and item.get("content_hash")
            ),
        }
    result["content_hash"] = "sha256:" + hashlib.sha256(
        _canonical_json(body).encode("utf-8")
    ).hexdigest()
    return result


def _decode_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def _assert_identity_rows(rows: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    identities: dict[str, tuple[int, str, str]] = {}
    for row in rows:
        old_id = str(row["operating_unit_id"])
        sku = str(row.get("parent_seller_sku") or "").strip()
        asin = str(row.get("parent_asin") or "").strip().upper()
        if not sku:
            raise RuntimeError(
                f"cannot migrate operating unit {old_id}: parent_seller_sku is missing"
            )
        identity = (int(row["shop_id"]), asin, sku)
        previous = identities.setdefault(old_id, identity)
        if previous != identity:
            raise RuntimeError(f"old operating unit {old_id} maps to multiple business keys")
        mapping[old_id] = _new_id(*identity)
    return mapping


def _identity_rows(connection: sa.Connection, tables: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in ("t_patrol_job", "t_patrol_asin_owner"):
        if table not in tables:
            continue
        rows.extend(
            dict(row)
            for row in connection.execute(
                sa.text(
                    f"SELECT operating_unit_id,shop_id,parent_asin,parent_seller_sku "
                    f"FROM `{table}`"
                )
            ).mappings()
        )
    return rows


def _assert_all_ids_mapped(
    connection: sa.Connection,
    tables: set[str],
    mapping: dict[str, str],
) -> None:
    for table in ID_TABLES:
        if table not in tables:
            continue
        ids = set(
            connection.execute(
                sa.text(f"SELECT DISTINCT operating_unit_id FROM `{table}`")
            ).scalars()
        )
        missing = ids - set(mapping) - set(mapping.values())
        if missing:
            raise RuntimeError(f"{table} contains operating-unit IDs without identity rows")


def _assert_no_conflicts(
    connection: sa.Connection,
    tables: set[str],
    mapping: dict[str, str],
) -> None:
    checks = {
        "t_patrol_job": ("batch_id",),
        "t_patrol_signal": ("issue_code", "child_scope_key"),
        "t_patrol_asin_owner": ("assignment_key",),
    }
    for table, key_columns in checks.items():
        if table not in tables:
            continue
        columns = ("operating_unit_id", *key_columns)
        rows = connection.execute(
            sa.text(f"SELECT {','.join(columns)} FROM `{table}`")
        ).mappings()
        seen: set[tuple[Any, ...]] = set()
        for row in rows:
            old_id = str(row["operating_unit_id"])
            key = (mapping.get(old_id, old_id),) + tuple(
                row[column] for column in key_columns
            )
            if key in seen:
                raise RuntimeError(f"{table} has duplicate rows under the new business key")
            seen.add(key)


def _update_json_columns(
    connection: sa.Connection,
    tables: set[str],
    mapping: dict[str, str],
) -> None:
    inspector = inspect(connection)
    for table, configured_columns in JSON_COLUMNS.items():
        if table not in tables:
            continue
        available = {column["name"] for column in inspector.get_columns(table)}
        columns = tuple(column for column in configured_columns if column in available)
        if not columns:
            continue
        primary_key = tuple(
            inspector.get_pk_constraint(table).get("constrained_columns") or ()
        )
        if not primary_key:
            raise RuntimeError(f"{table} must have a primary key for JSON migration")
        selected = (*primary_key, *columns)
        rows = list(
            connection.execute(
                sa.text(f"SELECT {','.join(selected)} FROM `{table}`")
            ).mappings()
        )
        for row_number, row in enumerate(rows, start=1):
            values: dict[str, Any] = {}
            for column in columns:
                original = _decode_json(row[column])
                replaced = _recompute_embedded_snapshot_hashes(
                    _replace_ids(original, mapping)
                )
                if replaced != original:
                    values[column] = _canonical_json(replaced)
            if not values:
                continue
            conditions = " AND ".join(f"`{column}` = :pk_{column}" for column in primary_key)
            assignments = ", ".join(f"`{column}` = :value_{column}" for column in values)
            parameters = {f"pk_{column}": row[column] for column in primary_key}
            parameters.update({f"value_{column}": value for column, value in values.items()})
            connection.execute(
                sa.text(f"UPDATE `{table}` SET {assignments} WHERE {conditions}"),
                parameters,
            )
            if table == "t_patrol_job" or row_number % 200 == 0:
                connection.commit()
        connection.commit()


def _update_snapshot_hashes(connection: sa.Connection, tables: set[str]) -> None:
    if "t_patrol_fact_snapshot" not in tables:
        return
    rows = connection.execute(
        sa.text(
            "SELECT s.snapshot_id,s.operating_unit_id,s.quality_status,s.source_refs_json,"
            "s.normalized_summary_json,j.shop_id,j.site_code,j.parent_asin,"
            "j.parent_seller_sku FROM t_patrol_fact_snapshot s "
            "JOIN t_patrol_run r ON r.run_id=s.run_id "
            "JOIN t_patrol_job j ON j.job_id=r.job_id"
        )
    ).mappings()
    domains = (
        "identity",
        "sales",
        "traffic",
        "profit",
        "inventory",
        "price",
        "quality",
        "execution_history",
    )
    for row in rows:
        unit = {
            "contract_version": CONTRACT_VERSION,
            "operating_unit_id": row["operating_unit_id"],
            "shop_id": int(row["shop_id"]),
            "site_code": row["site_code"],
            "parent_asin": row["parent_asin"],
            "parent_seller_sku": row["parent_seller_sku"],
        }
        normalized = _decode_json(row["normalized_summary_json"]) or {}
        if row["quality_status"] == "FAILED":
            body = {"operating_unit": unit, "domains": {}, "blocked": True}
        else:
            refs = _decode_json(row["source_refs_json"]) or []
            body = {
                "operating_unit": unit,
                "domains": {domain: normalized.get(domain, {}) for domain in domains},
                "source_content_hashes": sorted(
                    item["content_hash"]
                    for item in refs
                    if item.get("content_hash")
                ),
            }
        digest = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                "UPDATE t_patrol_fact_snapshot SET content_hash=:content_hash "
                "WHERE snapshot_id=:snapshot_id"
            ),
            {"content_hash": digest, "snapshot_id": row["snapshot_id"]},
        )


def _update_signal_dedup_keys(connection: sa.Connection, tables: set[str]) -> None:
    if "t_patrol_signal" not in tables:
        return
    rows = connection.execute(
        sa.text(
            "SELECT signal_id,operating_unit_id,issue_code,child_scope_key "
            "FROM t_patrol_signal"
        )
    ).mappings()
    for row in rows:
        raw = "\x1f".join(
            (row["operating_unit_id"], row["issue_code"], row["child_scope_key"])
        )
        connection.execute(
            sa.text(
                "UPDATE t_patrol_signal SET dedup_key=:dedup_key WHERE signal_id=:signal_id"
            ),
            {
                "dedup_key": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "signal_id": row["signal_id"],
            },
        )


def _update_outbox_hashes(connection: sa.Connection, tables: set[str]) -> None:
    if "t_patrol_delivery_outbox" not in tables:
        return
    rows = connection.execute(
        sa.text(
            "SELECT outbox_id,payload_json FROM t_patrol_delivery_outbox "
            "WHERE aggregate_type='INSPECTION_RUN'"
        )
    ).mappings()
    for row in rows:
        payload = _decode_json(row["payload_json"])
        hash_payload = dict(payload)
        hash_payload.pop("generated_at", None)
        digest = hashlib.sha256(_canonical_json(hash_payload).encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                "UPDATE t_patrol_delivery_outbox SET payload_hash=:payload_hash "
                "WHERE outbox_id=:outbox_id"
            ),
            {"payload_hash": digest, "outbox_id": row["outbox_id"]},
        )


def _update_batch_hashes(connection: sa.Connection, tables: set[str]) -> None:
    if not {"t_patrol_batch", "t_patrol_job"}.issubset(tables):
        return
    batch_ids = connection.execute(sa.text("SELECT batch_id FROM t_patrol_batch")).scalars()
    for batch_id in batch_ids:
        rows = connection.execute(
            sa.text(
                "SELECT operating_unit_id,payload_json FROM t_patrol_job "
                "WHERE batch_id=:batch_id"
            ),
            {"batch_id": batch_id},
        ).mappings()
        bindings: list[tuple[str, list[int]]] = []
        for row in rows:
            payload = _decode_json(row["payload_json"]) or {}
            owners = (payload.get("binding") or {}).get("owner_user_ids") or []
            bindings.append((row["operating_unit_id"], sorted(int(item) for item in owners)))
        digest = hashlib.sha256(_canonical_json(sorted(bindings)).encode("utf-8")).hexdigest()
        batch = connection.execute(
            sa.text(
                "SELECT trigger_type,business_date,rule_bundle_version,idempotency_key "
                "FROM t_patrol_batch WHERE batch_id=:batch_id"
            ),
            {"batch_id": batch_id},
        ).mappings().one()
        idempotency_key = batch["idempotency_key"]
        if batch["trigger_type"] == "DAILY_SCHEDULE":
            idempotency_key = (
                f"DAILY_SCHEDULE:{batch['business_date'].isoformat()}:"
                f"{digest}:{batch['rule_bundle_version']}"
            )
        elif batch["trigger_type"] == "OBSERVATION_DUE":
            parts = str(idempotency_key).split(":")
            if len(parts) < 6:
                raise RuntimeError("observation batch has an invalid idempotency key")
            parts[-2] = digest
            idempotency_key = ":".join(parts)
        connection.execute(
            sa.text(
                "UPDATE t_patrol_batch SET scope_hash=:scope_hash,"
                "idempotency_key=:idempotency_key WHERE batch_id=:batch_id"
            ),
            {
                "scope_hash": digest,
                "idempotency_key": idempotency_key,
                "batch_id": batch_id,
            },
        )
def _drop_old_config_uniques(connection: sa.Connection) -> None:
    inspector = inspect(connection)
    for constraint in inspector.get_unique_constraints("t_ops_operating_unit_config"):
        columns = tuple(constraint.get("column_names") or ())
        name = constraint.get("name")
        if columns != ("shop_id", "site_code", "parent_asin") or not name:
            continue
        if not IDENTIFIER_PATTERN.fullmatch(name):
            raise RuntimeError("operating-unit config has an unsafe constraint name")
        connection.execute(sa.text(f"ALTER TABLE t_ops_operating_unit_config DROP INDEX `{name}`"))


def _migrate_config_table(connection: sa.Connection, tables: set[str]) -> None:
    if "t_ops_operating_unit_config" not in tables:
        return
    missing = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM t_ops_operating_unit_config "
            "WHERE parent_seller_sku IS NULL OR TRIM(parent_seller_sku)=''"
        )
    ).scalar_one()
    if missing:
        raise RuntimeError("t_ops_operating_unit_config contains missing parent_seller_sku")
    duplicate = connection.execute(
        sa.text(
            "SELECT 1 FROM t_ops_operating_unit_config "
            "GROUP BY shop_id,parent_asin,parent_seller_sku HAVING COUNT(*)>1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError("t_ops_operating_unit_config conflicts under the new business key")
    _drop_old_config_uniques(connection)
    connection.execute(
        sa.text(
            "ALTER TABLE t_ops_operating_unit_config "
            "MODIFY parent_seller_sku VARCHAR(128) NOT NULL"
        )
    )
    existing_names = {
        item.get("name")
        for item in inspect(connection).get_unique_constraints("t_ops_operating_unit_config")
    }
    if "ux_ops_operating_unit_business_key" not in existing_names:
        connection.execute(
            sa.text(
                "ALTER TABLE t_ops_operating_unit_config ADD CONSTRAINT "
                "ux_ops_operating_unit_business_key UNIQUE "
                "(shop_id,parent_asin,parent_seller_sku)"
            )
        )


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    mapping = _assert_identity_rows(_identity_rows(connection, tables))
    _assert_all_ids_mapped(connection, tables, mapping)
    _assert_no_conflicts(connection, tables, mapping)

    _update_json_columns(connection, tables, mapping)
    for table in ID_TABLES:
        if table not in tables:
            continue
        for row_number, (old_id, new_id) in enumerate(mapping.items(), start=1):
            if old_id == new_id:
                continue
            connection.execute(
                sa.text(
                    f"UPDATE `{table}` SET operating_unit_id=:new_id "
                    "WHERE operating_unit_id=:old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )
            if table == "t_patrol_job" or row_number % 200 == 0:
                connection.commit()
        connection.commit()

    _update_signal_dedup_keys(connection, tables)
    connection.commit()
    _update_snapshot_hashes(connection, tables)
    connection.commit()
    _update_outbox_hashes(connection, tables)
    connection.commit()
    _update_batch_hashes(connection, tables)
    connection.commit()

    if "t_patrol_job" in tables:
        op.alter_column(
            "t_patrol_job",
            "parent_seller_sku",
            existing_type=sa.String(128),
            nullable=False,
        )
    if "t_patrol_asin_owner" in tables:
        op.alter_column(
            "t_patrol_asin_owner",
            "parent_seller_sku",
            existing_type=sa.String(128),
            nullable=False,
        )
        op.drop_index("ix_patrol_asin_owner_business", table_name="t_patrol_asin_owner")
        op.create_index(
            "ix_patrol_asin_owner_business",
            "t_patrol_asin_owner",
            ["shop_id", "parent_asin", "parent_seller_sku"],
        )
    _migrate_config_table(connection, tables)


def downgrade() -> None:
    raise RuntimeError("the control-center operating-unit business key is irreversible")
