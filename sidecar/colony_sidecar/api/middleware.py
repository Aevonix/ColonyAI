"""API key authentication middleware for Colony sidecar."""

from __future__ import annotations

import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the correct Bearer token.

    Skips auth for health checks and the OpenAPI spec endpoint.
    If ``COLONY_API_KEY`` is not set, all requests are allowed (dev mode).
    """

    def __init__(self, app, api_key: str | None = None) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        # Always allow health and docs
        path = request.url.path
        if path in ("/v1/host/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        # No key configured → open access (dev mode)
        if not self._api_key:
            return await call_next(request)

        # Check Authorization header
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token == self._api_key:
                return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )
