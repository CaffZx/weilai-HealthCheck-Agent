from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from core.result_envelope import InspectionFeedbackEvent
from integrations.feedback import FeedbackInboxService, FeedbackResult
from web.backend.deps import get_feedback_inbox, require_feedback_access

router = APIRouter(prefix="/internal/v1/patrol", tags=["patrol-feedback-v1"])


@router.post("/feedback-events", summary="幂等接收中控处理反馈")
async def consume_feedback(
    event: InspectionFeedbackEvent,
    _: Annotated[None, Depends(require_feedback_access)],
    inbox: Annotated[FeedbackInboxService, Depends(get_feedback_inbox)],
) -> dict:
    result: FeedbackResult = inbox.consume(event)
    body = result.to_json()
    body["duplicate"] = result.duplicate
    if result.error_code == "IDEMPOTENCY_CONFLICT":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=body)
    if not result.accepted:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=body)
    return body
