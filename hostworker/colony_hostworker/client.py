"""Strict loopback client for the governed-action execution endpoint.

The endpoint contract this client relies on is part of the safety boundary:

* ``PUT /v1/host/actions/{action_id}`` durably reserves the immutable action
  as ``executing`` before any effect is attempted.  That reservation permits
  at most one mutation attempt and is never automatically replayed — so this
  client NEVER retries a mutation.  A ``PUT`` that fails, times out, or
  returns garbage leaves the outcome unknown, and the only permitted
  follow-up is read-only observation.
* ``GET /v1/host/actions/{action_id}`` is side-effect-free and returns the
  endpoint's stable digest-bound projection of the action.

The client is loopback-only (see :func:`colony_hostworker._private_io.\
loopback_origin`), uses a redirect-refusing opener so the bearer credential
can never be replayed to another origin, and decodes responses through the
bounded strict JSON reader.  It validates only the request document's outer
identity; semantic validation of responses belongs to the worker
(:func:`colony_hostworker.worker.validate_execution_result`).
"""

from __future__ import annotations

import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from ._private_io import loopback_origin, read_private_json, strict_json_bytes
from .contract import (
    ACTION_ID_RE,
    EXECUTION_REQUEST_FIELDS,
    EXECUTION_REQUEST_MAX_BYTES,
    EXECUTION_REQUEST_SCHEMA,
    EXECUTION_RESULT_MAX_BYTES,
    canonical_json_utf8,
)


class GovernedActionClientError(RuntimeError):
    """The execution endpoint is unavailable or returned invalid data."""


# The one principal the endpoint accepts for host-worker execution.  It is a
# public wire string shared with the sidecar's independent validator
# (``GOVERNED_ACTION_PRINCIPAL``); never rename it.
WORKER_PRINCIPAL = "host-action-worker"

CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")

_ACTIONS_PATH = "/v1/host/actions/"


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so credentials never leave the pinned origin.

    Python's default redirect handler copies ordinary request headers —
    including ``Authorization`` — to the redirect target.  A loopback service
    boundary must instead surface the redirect to its caller as an error.
    """

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


def build_no_redirect_opener():
    """Build a proxy-free, redirect-refusing ``urllib`` opener."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirectHandler()
    )


@dataclass(frozen=True)
class ClientCredential:
    """Dedicated client identity for only the governed-action endpoint.

    Loaded exclusively from an owner-only mode-0600 regular file so a
    world-readable secret, a symlink swap, or a group-readable deploy
    artifact fails closed at startup instead of leaking authority.
    """

    principal: str
    credential_id: str
    secret: str = field(repr=False)

    @classmethod
    def load(cls, path: str) -> "ClientCredential":
        _target, _raw, document = read_private_json(
            path,
            label="governed action credential",
            error=GovernedActionClientError,
        )
        fields = {"version", "principal", "credential_id", "secret"}
        if not isinstance(document, Mapping) or set(document) != fields:
            raise GovernedActionClientError(
                "governed action credential fields are invalid"
            )
        if (
            isinstance(document.get("version"), bool)
            or document.get("version") != 1
            or document.get("principal") != WORKER_PRINCIPAL
        ):
            raise GovernedActionClientError(
                "governed action credential principal is invalid"
            )
        credential_id = document.get("credential_id")
        secret = document.get("secret")
        if not isinstance(credential_id, str) or not CREDENTIAL_ID_RE.fullmatch(
            credential_id
        ):
            raise GovernedActionClientError(
                "governed action credential ID is invalid"
            )
        if (
            not isinstance(secret, str)
            or not 32 <= len(secret) <= 512
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E
                for character in secret
            )
        ):
            raise GovernedActionClientError(
                "governed action credential secret is invalid"
            )
        return cls(
            principal=WORKER_PRINCIPAL,
            credential_id=credential_id,
            secret=secret,
        )


class GovernedActionClient:
    """Credential-bound client for one loopback governed-action origin.

    ``execute`` issues exactly one ``PUT`` per call and NEVER retries it —
    the caller's state machine owns the one-mutation guarantee and must treat
    any failure here as an unknown outcome to be resolved by ``observe``
    only.  ``observe`` issues a side-effect-free ``GET``.
    """

    def __init__(
        self,
        origin: str,
        credential: ClientCredential,
        *,
        opener=None,
        timeout: float = 5.0,
    ) -> None:
        if not isinstance(credential, ClientCredential):
            raise GovernedActionClientError(
                "governed action credential is invalid"
            )
        if isinstance(timeout, bool):
            raise GovernedActionClientError("governed action timeout is invalid")
        try:
            request_timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as error:
            raise GovernedActionClientError(
                "governed action timeout is invalid"
            ) from error
        if not math.isfinite(request_timeout) or not 0.1 <= request_timeout <= 30.0:
            raise GovernedActionClientError("governed action timeout is invalid")
        candidate = build_no_redirect_opener() if opener is None else opener
        if not hasattr(candidate, "open") or not callable(candidate.open):
            raise GovernedActionClientError(
                "governed action HTTP opener is invalid"
            )
        self.origin = loopback_origin(origin, error=GovernedActionClientError)
        self.credential = credential
        self.timeout = request_timeout
        self.opener = candidate

    @staticmethod
    def _action_id(request: Mapping[str, Any]) -> str:
        if (
            not isinstance(request, Mapping)
            or set(request) != EXECUTION_REQUEST_FIELDS
            or request.get("schema") != EXECUTION_REQUEST_SCHEMA
            or isinstance(request.get("version"), bool)
            or request.get("version") != 1
        ):
            raise GovernedActionClientError(
                "governed action request fields are invalid"
            )
        action_id = request.get("action_id")
        if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
            raise GovernedActionClientError("governed action ID is invalid")
        return action_id

    def _request(self, method: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        action_id = self._action_id(request)
        data = None
        if method == "PUT":
            try:
                data = canonical_json_utf8(dict(request)).encode("utf-8")
            except (
                TypeError,
                ValueError,
                UnicodeError,
                OverflowError,
                RecursionError,
            ) as error:
                raise GovernedActionClientError(
                    "governed action request is invalid"
                ) from error
            if len(data) > EXECUTION_REQUEST_MAX_BYTES:
                raise GovernedActionClientError(
                    "governed action request is too large"
                )
        elif method != "GET":
            raise GovernedActionClientError(
                "governed action HTTP method is invalid"
            )
        target = self.origin + _ACTIONS_PATH + urllib.parse.quote(
            action_id, safe="-"
        )
        outbound = urllib.request.Request(
            target,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.credential.secret,
                "X-Colony-Principal": self.credential.principal,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(outbound, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if isinstance(status, bool) or status != 200:
                    raise GovernedActionClientError(
                        "governed action response was rejected"
                    )
                raw = response.read(EXECUTION_RESULT_MAX_BYTES + 1)
                if not isinstance(raw, bytes) or len(raw) > EXECUTION_RESULT_MAX_BYTES:
                    raise GovernedActionClientError(
                        "governed action response is too large"
                    )
        except GovernedActionClientError:
            raise
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as error:
            raise GovernedActionClientError(
                "governed action service is unavailable"
            ) from error
        except Exception as error:
            raise GovernedActionClientError(
                "governed action exchange failed"
            ) from error
        value = strict_json_bytes(
            raw,
            maximum=EXECUTION_RESULT_MAX_BYTES,
            error=GovernedActionClientError,
        )
        if not isinstance(value, Mapping):
            raise GovernedActionClientError(
                "governed action response must be an object"
            )
        return value

    def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Issue THE one mutation PUT for this request.  Never retried."""

        return self._request("PUT", request)

    def observe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Issue one side-effect-free reconciliation GET."""

        return self._request("GET", request)


__all__ = (
    "CREDENTIAL_ID_RE",
    "ClientCredential",
    "GovernedActionClient",
    "GovernedActionClientError",
    "NoRedirectHandler",
    "WORKER_PRINCIPAL",
    "build_no_redirect_opener",
)
