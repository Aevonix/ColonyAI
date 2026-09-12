"""Source settlement: resolving a concern settles what the concern points at.

A concern raised from a durable source (an overdue commitment, an anomaly, a
stale goal) carries that source in its ``sources`` list as ``"<kind>:<id>"``.
Resolving only the concern leaves the source open, and whatever ingests that
source re-raises the concern on the next tick — the owner's resolve silently
undone minutes later. The registry maps a source kind to a settle callback so
every surface (owner deck, agent tool, MCP, API) closes the whole chain with
one call.

Deployment-agnostic: the server wires settlers for whatever stores it runs;
unknown source kinds are skipped, and one failing settler never blocks the
others.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

Settler = Callable[..., Optional[Dict[str, Any]]]

_SETTLERS: Dict[str, Settler] = {}
_RETRY_SAFE: set[str] = set()
_REGISTRY_LOCK = threading.RLock()


class SettlementRetryUnsafe(ValueError):
    """One or more sources lack an exact operation-bound retry contract."""

    def __init__(self, sources: List[str]) -> None:
        super().__init__("cascade recovery requires retry-safe source settlers")
        self.sources = list(sources)


class _SettlementOperationUnverified(ValueError):
    settlement_error_code = "operation_unverified"


def source_operation_id(operation_root: str, source: str) -> str:
    """Derive the stable per-intent/per-source idempotency identity."""

    root = str(operation_root or "").strip()
    ref = str(source or "").strip()
    if not root or not ref:
        raise ValueError("source settlement operation identity is incomplete")
    digest = hashlib.sha256(f"{root}\0{ref}".encode("utf-8")).hexdigest()
    return f"concern-source-operation:{digest}"


def register_settler(
    kind: str,
    fn: Settler,
    *,
    retry_safe: bool = False,
) -> None:
    """Register the settle callback for a source kind (e.g. "commitment").

    Retry-safe callbacks additionally receive a stable ``operation_id`` and
    must bind it together with outcome, note, and resolver to the durable
    transition.  Their detail must attest those exact fields.  This is a
    stronger contract than merely returning the same terminal state twice.
    """
    if type(retry_safe) is not bool:
        raise ValueError("settler retry_safe must be boolean")
    with _REGISTRY_LOCK:
        _SETTLERS[kind] = fn
        if retry_safe:
            _RETRY_SAFE.add(kind)
        else:
            _RETRY_SAFE.discard(kind)


def registered_kinds() -> List[str]:
    with _REGISTRY_LOCK:
        return sorted(_SETTLERS)


def _settle_sources(
    sources: List[str],
    *,
    callbacks: Mapping[str, Settler],
    retry_safe_kinds: set[str],
    outcome: str,
    note: str,
    resolved_by: str,
    operation_root: Optional[str],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for src in sources:
        kind, _, source_id = str(src).partition(":")
        fn = callbacks.get(kind)
        if fn is None or not source_id:
            continue
        entry: Dict[str, Any] = {"source": src, "settled": False}
        operation_id = None
        try:
            kwargs: Dict[str, Any] = {
                "outcome": outcome,
                "note": note,
                "resolved_by": resolved_by,
            }
            if operation_root is not None and kind in retry_safe_kinds:
                operation_id = source_operation_id(operation_root, src)
                kwargs["operation_id"] = operation_id
            detail = fn(source_id, **kwargs)
            if detail is not None:
                if operation_id is not None:
                    note_digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
                    if not isinstance(detail, Mapping) or any((
                        detail.get("operation_id") != operation_id,
                        detail.get("outcome") != outcome,
                        detail.get("note_digest") != note_digest,
                        detail.get("resolved_by") != resolved_by,
                    )):
                        raise _SettlementOperationUnverified(
                            "retry-safe settler did not attest the bound operation"
                        )
                entry["settled"] = True
                if isinstance(detail, Mapping):
                    entry.update({
                        key: value for key, value in detail.items()
                        if key not in {
                            "source", "settled", "error", "error_digest",
                        }
                    })
        except Exception as exc:
            logger.warning("settler for %s failed (%s)", src, type(exc).__name__)
            error_code = getattr(exc, "settlement_error_code", "settler_error")
            if error_code not in {
                "operation_conflict", "operation_unverified", "settler_error",
            }:
                error_code = "settler_error"
            entry["error"] = error_code
            entry["error_digest"] = hashlib.sha256(
                str(exc).encode("utf-8", errors="replace")
            ).hexdigest()
        results.append(entry)
    return results


def settle_sources(
    sources: Optional[List[str]],
    *,
    outcome: str = "done",
    note: str = "",
    resolved_by: str = "owner",
    operation_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Settle every recognized source reference. Returns one entry per source
    that had a registered settler, with ``settled`` and any settler detail."""
    with _REGISTRY_LOCK:
        callbacks = dict(_SETTLERS)
        retry_safe_kinds = set(_RETRY_SAFE)
    return _settle_sources(
        list(sources or []),
        callbacks=callbacks,
        retry_safe_kinds=retry_safe_kinds,
        outcome=outcome,
        note=note,
        resolved_by=resolved_by,
        operation_root=operation_root,
    )


def retry_safe_settle_sources(
    sources: Optional[List[str]],
    *,
    operation_root: str,
    outcome: str = "done",
    note: str = "",
    resolved_by: str = "owner",
) -> List[Dict[str, Any]]:
    """Explicitly retry only sources with exact operation-bound semantics.

    The complete source set is validated before the first callback.  Unknown,
    malformed, or merely state-idempotent settlers are rejected together and
    remain pending for explicit reconciliation.
    """

    refs = list(sources or [])
    unsafe: List[str] = []
    with _REGISTRY_LOCK:
        callbacks = dict(_SETTLERS)
        retry_safe_kinds = set(_RETRY_SAFE)
    for src in refs:
        kind, separator, source_id = str(src).partition(":")
        if (
            not separator or not source_id or kind not in callbacks
            or kind not in retry_safe_kinds
        ):
            unsafe.append(str(src))
    if unsafe:
        raise SettlementRetryUnsafe(unsafe)
    return _settle_sources(
        refs,
        callbacks=callbacks,
        retry_safe_kinds=retry_safe_kinds,
        outcome=outcome,
        note=note,
        resolved_by=resolved_by,
        operation_root=operation_root,
    )
