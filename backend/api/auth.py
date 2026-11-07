"""API-key auth hook — auth-READY, disabled by default (§§66, 77).

Enable by setting the AETHER_API_KEY environment variable to a non-empty
value. When enabled, protected routes require the header::

    X-API-Key: <the key>

When disabled (default), every request passes through untouched and the API
is open — the current v1 behavior.

No user accounts, no sessions: this is a single shared service key for
operator tooling / the Android shell. Routes opt in with::

    from fastapi import Depends
    from backend.api.auth import require_api_key

    @router.get("/v1/admin/ping", dependencies=[Depends(require_api_key)])
    def ping(): ...

Alternatively use ApiKeyMiddleware for blanket enforcement on /v1/*.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

API_KEY_ENV = "AETHER_API_KEY"
API_KEY_HEADER = "X-API-Key"

_api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def configured_key() -> Optional[str]:
    """The enabled key, or None when auth is disabled."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def auth_enabled() -> bool:
    """True only when AETHER_API_KEY is set to a non-empty value."""
    return configured_key() is not None


def check_key(provided: Optional[str]) -> Optional[str]:
    """Validate a presented key. Returns it on success, raises 401 otherwise.

    No-op (returns None) when auth is disabled.
    """
    expected = configured_key()
    if expected is None:
        return None
    if not provided or provided != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Send header X-API-Key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return provided


async def require_api_key(request: Request,
                          presented: Optional[str] = Depends(_api_key_header)
                          ) -> Optional[str]:
    """FastAPI dependency: enforce the API key when auth is enabled."""
    return check_key(presented)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Optional blanket enforcement for /v1/* paths (health/status/metrics exempt).

    NOT registered by default — add it in app.py only when the deployment
    wants every /v1 route behind the key.
    """

    async def dispatch(self, request: Request,
                       call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/v1"):
            try:
                check_key(request.headers.get(API_KEY_HEADER))
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail},
                                    status_code=exc.status_code,
                                    headers=exc.headers or {})
        return await call_next(request)
