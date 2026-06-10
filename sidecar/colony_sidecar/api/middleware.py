"""API key authentication + request-size middleware for Colony sidecar."""

from __future__ import annotations

import hmac
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Default cap on request body size (10 MiB). Oversized uploads are rejected
# at the middleware layer before the handler buffers them. Override via
# COLONY_MAX_BODY_BYTES in the environment.
_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024


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
# and must never be anonymously reachable. Agent registration / connect
# endpoints accept caller-supplied `is_primary` and capability lists, so
# they must fail closed in dev mode rather than letting an unauthenticated
# caller register a fully-privileged primary agent.
_ALWAYS_AUTH_REQUIRED = frozenset({
    "/v1/host/configure",
    "/v1/host/agents/register",
    "/v1/host/agents/connect",
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

        # Both header styles are in active use: the gateway sends
        # ``Authorization: Bearer``, the poller/queue-worker scripts send
        # ``X-API-Key`` (and advertise it to agents in job payloads).
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-API-Key", "")
        if token and hmac.compare_digest(
            token.encode("utf-8"), self._api_key.encode("utf-8")
        ):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests that declare a body larger than ``max_bytes``.

    Short-circuits before the handler reads the payload, so oversized uploads
    cannot be used to exhaust memory. Requests without a ``Content-Length``
    header (e.g. chunked transfer encoding) are allowed to pass — FastAPI's
    own buffer limits still apply downstream.
    """

    def __init__(self, app, max_bytes: int = _DEFAULT_MAX_BODY_BYTES) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                length = int(cl)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
            if length > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Request body exceeds limit "
                            f"({length} > {self._max_bytes} bytes)"
                        )
                    },
                )
        return await call_next(request)
