"""Health and system endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness probe — always 200 if the process is alive."""
    return JSONResponse(
        {
            "status": "healthy",
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
