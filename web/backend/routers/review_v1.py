"""已退役的中控主动复盘接口占位。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/internal/v1/reviews", tags=["reviews-v1-deprecated"])

RETIRED_DETAIL = {
    "code": "LEGACY_REVIEW_RETIRED",
    "message": (
        "control-center initiated review is retired; use feedback events and "
        "the patrol scheduler's due rescan flow"
    ),
}


@router.post("", status_code=status.HTTP_410_GONE, deprecated=True)
async def request_review() -> None:
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=RETIRED_DETAIL)


@router.get("/{job_id}", status_code=status.HTTP_410_GONE, deprecated=True)
async def get_review_job(job_id: str) -> None:
    del job_id
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=RETIRED_DETAIL)
