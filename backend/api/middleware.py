"""API middleware: rate limiting + request logging (§§77, 108).

RateLimitMiddleware — in-memory token bucket per client IP, applied to /v1/*
only (health/status/metrics stay unthrottled for ops). Default quota:
120 requests/minute per IP with a matching burst. State is per-process;
on a single-host SQLite deployment this is the whole deployment. A 429
response includes a JSON body and a Retry-After header.

RequestLoggingMiddleware — one log line per request with method, path,
status, duration and client ip. Emits a JSON object when AETHER_LOG_JSON=1,
a compact text line otherwise (see backend/common/logging_setup.py).
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.api")

RATE_LIMIT_RPM = int(os.environ.get("AETHER_RATE_LIMIT_RPM", "120"))
RATE_LIMIT_BURST = int(os.environ.get("AETHER_RATE_LIMIT_BURST",
                                      str(RATE_LIMIT_RPM)))


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, tokens: float, updated: float):
        self.tokens = tokens
        self.updated = updated


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter, per client IP, for /v1/* paths."""

    def __init__(self, app, requests_per_minute: int = RATE_LIMIT_RPM,
                 burst: int | None = None, exempt_prefixes=("/health", "/status", "/metrics")):
        super().__init__(app)
        self.rpm = max(int(requests_per_minute), 1)
        self.capacity = float(burst if burst is not None else self.rpm)
        self.exempt = tuple(exempt_prefixes)
        self._buckets: Dict[str, _Bucket] = {}

    def _refill(self, bucket: _Bucket, now: float) -> None:
        elapsed = now - bucket.updated
        if elapsed > 0:
            bucket.tokens = min(self.capacity,
                                bucket.tokens + elapsed * (self.rpm / 60.0))
            bucket.updated = now

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request,
                       call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith(self.exempt) or not request.url.path.startswith("/v1"):
            return await call_next(request)
        now = time.monotonic()
        ip = self._client_ip(request)
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = self._buckets[ip] = _Bucket(self.capacity, now)
        self._refill(bucket, now)
        if bucket.tokens < 1.0:
            retry_after = max(1, int((1.0 - bucket.tokens) / (self.rpm / 60.0)) + 1)
            return JSONResponse(
                {"detail": "rate limit exceeded",
                 "message": "Too many requests. Slow down and retry."},
                status_code=429,
                headers={"Retry-After": str(retry_after)})
        bucket.tokens -= 1.0
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """One structured log line per request."""

    async def dispatch(self, request: Request,
                       call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        client = request.client.host if request.client else "unknown"
        record = {
            "event": "http_request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "client_ip": client,
        }
        if os.environ.get("AETHER_LOG_JSON", "0") == "1":
            log.info(json.dumps(record))
        else:
            log.info("%s %s -> %s (%sms) ip=%s", record["method"], record["path"],
                     record["status"], record["duration_ms"], record["client_ip"])
        return response
