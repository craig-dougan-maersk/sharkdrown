"""Health and system endpoints."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Request
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


@router.get("/api/providers/health")
async def providers_health(request: Request) -> JSONResponse:
    """Ping each configured provider and report latency/status.

    Returns a JSON object keyed by provider name with:
      - status: "ok" | "error"
      - latency_ms: round-trip time (null on error)
      - error: error message (null on success)
    """
    import httpx

    providers: dict = getattr(request.app.state, "providers", {})
    results: dict = {}

    for name, provider in providers.items():
        base_url = getattr(provider, "_base_url", None)
        if not base_url:
            results[name] = {"status": "unknown", "latency_ms": None, "error": "no base URL"}
            continue
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(base_url, follow_redirects=True)
            latency = round((time.perf_counter() - start) * 1000)
            results[name] = {
                "status": "ok" if resp.status_code < 500 else "error",
                "latency_ms": latency,
                "http_status": resp.status_code,
                "error": None,
            }
        except (httpx.HTTPError, OSError) as exc:
            latency = round((time.perf_counter() - start) * 1000)
            results[name] = {
                "status": "error",
                "latency_ms": latency,
                "error": str(exc),
            }

    return JSONResponse({"providers": results, "timestamp": datetime.now(UTC).isoformat()})
