from __future__ import annotations

import argparse
import json

import sqlalchemy as sa

from core.control_center_contracts import SubmitPatrolBatchRequest, _to_platform_site_code
from core.enums import DeliveryStatus
from integrations.control_center_delivery import AGGREGATE_TYPE, _payload_hash
from integrations.database import create_database_engine
from integrations.repositories.tables import patrol_delivery_outbox


def requeue(outbox_id: str) -> str:
    engine = create_database_engine()
    try:
        with engine.begin() as connection:
            row = connection.execute(
                sa.select(patrol_delivery_outbox).where(
                    patrol_delivery_outbox.c.outbox_id == outbox_id,
                    patrol_delivery_outbox.c.aggregate_type == AGGREGATE_TYPE,
                    patrol_delivery_outbox.c.status == DeliveryStatus.DEAD.value,
                ).with_for_update()
            ).mappings().one_or_none()
            if row is None:
                raise RuntimeError("specified outbox is not a DEAD control-center batch")
            payload = row["payload_json"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            request = SubmitPatrolBatchRequest.model_validate(payload)
            units = [
                unit.model_copy(update={
                    "listing": unit.listing.model_copy(update={
                        "site_code": _to_platform_site_code(unit.listing.site_code),
                    })
                })
                for unit in request.units
            ]
            corrected = request.model_copy(update={"units": units})
            corrected_payload = corrected.model_dump(mode="json", by_alias=True)
            connection.execute(
                sa.update(patrol_delivery_outbox)
                .where(patrol_delivery_outbox.c.outbox_id == outbox_id)
                .values(
                    payload_hash=_payload_hash(corrected),
                    payload_json=corrected_payload,
                    status=DeliveryStatus.PENDING.value,
                    retry_count=0,
                    next_retry_at=None,
                    locked_by=None,
                    locked_at=None,
                    last_error=None,
                    delivery_ack_json=None,
                )
            )
            return outbox_id
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Requeue one corrected DEAD control-center batch")
    parser.add_argument("--outbox-id", required=True)
    args = parser.parse_args()
    print(f"requeued outbox={requeue(args.outbox_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
