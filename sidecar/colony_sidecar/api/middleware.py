"""API key authentication middleware for Colony sidecar."""

from __future__ import annotations

import hmac
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Endpoints reachable without a key, even in "dev mode" (no COLONY_API_KEY).
# Health + docs keep the first-run wizard smooth; everything else (including
# /configure, which accepts LLM credentials) requires an explicit key.
_DEV_MODE_ALLOWED = frozenset({
    "/v1/host/health",
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Routes that must never be served without an API key, regardless of
# COLONY_API_KEY presence — these accept or return credential-grade state
# and must never be anonymously reachable.
_ALWAYS_AUTH_REQUIRED = frozenset({
    "/v1/host/configure",
})


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the correct Bearer token.

    Without ``COLONY_API_KEY`` set, only ``_DEV_MODE_ALLOWED`` paths are
    reachable; ``_ALWAYS_AUTH_REQUIRED`` paths fail closed with 503 so an
    operator cannot accidentally expose credential-handling endpoints.
    """

    def __init__(self, app, api_key: str | None = None) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in _DEV_MODE_ALLOWED:
            return await call_next(request)

        if not self._api_key:
            if path in _ALWAYS_AUTH_REQUIRED:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": (
                            "Set COLONY_API_KEY in the sidecar environment "
                            "to enable this endpoint."
                        )
                    },
                )
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if hmac.compare_digest(
                token.encode("utf-8"), self._api_key.encode("utf-8")
            ):
                return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )
