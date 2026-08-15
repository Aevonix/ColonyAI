"""Harness contract and fixtures for the store conformance suite.

A host proves its :class:`~colony_hostworker.store.ActionStore` adapter by
implementing :class:`StoreHarness` — the store under test plus the two
ingress operations the suite needs (proposing an action, attaching a gate
receipt) and a controllable clock — and passing a factory for it to
:func:`colony_hostworker.conformance.run_store_conformance`.

The clock MUST be the same clock the store judges time with (invariant
I11): several cases advance it to prove point-of-use expiry, lease theft,
and observation deadlines.

Everything here is stdlib-only; the suite runs without pytest so it can be
executed inside a host's own deployment checks
(``python -m colony_hostworker.conformance``).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from ..contract import INTENT_ENVELOPE_SCHEMA
from ..gate import GRANT_BINDING_METHOD, GRANT_UNLIMITED_SENTINEL
from ..intent import HermesToolActionIntentV1
from ..sqlite_store import SqliteActionStore


class ManualClock:
    """Deterministic, explicitly advanced clock for conformance runs."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now

    def __call__(self) -> float:
        return self._now


@runtime_checkable
class StoreHarness(Protocol):
    """One store under test plus the ingress the suite drives it with.

    ``store`` must implement :class:`~colony_hostworker.store.ActionStore`.
    ``propose`` and ``add_gate`` are the host's ingress equivalents (however
    they are implemented in production); ``add_gate`` returns
    ``(action, gate_receipt)``.  ``now``/``advance`` control the SAME clock
    the store reads.  ``close`` releases resources.
    """

    store: Any

    def now(self) -> float:
        ...

    def advance(self, seconds: float) -> float:
        ...

    def propose(
        self,
        *,
        idempotency_key: str,
        source: str,
        source_ref: str,
        action_type: str,
        payload: Any,
    ) -> Mapping[str, Any]:
        ...

    def add_gate(
        self,
        action_id: str,
        evidence: Mapping[str, Any],
        *,
        receipt_key: str = "owner-gate",
        external_id: str | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        ...

    def close(self) -> None:
        ...


HarnessFactory = Callable[[], StoreHarness]


class SqliteStoreHarness:
    """Reference harness: :class:`SqliteActionStore` on a throwaway path.

    ``store_class`` exists so the suite's own tests can prove the suite
    CATCHES deliberately broken stores; hosts testing a different store
    write their own harness instead.
    """

    def __init__(
        self,
        directory: str | None = None,
        *,
        store_class: type[SqliteActionStore] = SqliteActionStore,
    ) -> None:
        self._temp = (
            tempfile.mkdtemp(prefix="colony-hostworker-conformance-")
            if directory is None
            else None
        )
        base = self._temp if directory is None else directory
        self.clock = ManualClock()
        self.store = store_class(
            os.path.join(base, "governed-actions.sqlite3"), clock=self.clock
        )

    def now(self) -> float:
        return self.clock.now()

    def advance(self, seconds: float) -> float:
        return self.clock.advance(seconds)

    def propose(self, *, idempotency_key, source, source_ref, action_type, payload):
        return self.store.propose(
            idempotency_key,
            source,
            action_type,
            payload,
            source_ref=source_ref,
        )

    def add_gate(self, action_id, evidence, *, receipt_key="owner-gate", external_id=None):
        return self.store.gate(
            action_id,
            evidence,
            receipt_key=receipt_key,
            external_id=external_id,
        )

    def close(self) -> None:
        self.store.close()
        if self._temp:
            shutil.rmtree(self._temp, ignore_errors=True)


def sqlite_harness() -> SqliteStoreHarness:
    """Factory for the reference harness (used by the module runner)."""

    return SqliteStoreHarness()


# --------------------------------------------------------------- fixtures


def build_intent(
    *,
    tool_name: str = "colony_create_commitment",
    args: Mapping[str, Any] | None = None,
    seed: str | None = None,
) -> HermesToolActionIntentV1:
    """One valid governed intent with a unique call identity per ``seed``."""

    seed = seed or uuid.uuid4().hex[:12]
    if args is None:
        args = {"description": "conformance fixture commitment %s" % seed}
    context = {
        "api_request_id": "req-%s" % seed,
        "authority_lane": "owner",
        "contact_id": "contact-conformance",
        "platform": "conformance",
        "sender_id": "owner:conformance",
        "session_id": "sess-%s" % seed,
        "task_id": "",
        "tool_call_id": "call-%s" % seed,
        "turn_id": "turn-%s" % seed,
    }
    return HermesToolActionIntentV1.build(
        tool_name=tool_name, args=args, context=context
    )


def build_envelope(intent: HermesToolActionIntentV1) -> dict[str, Any]:
    return {
        "schema": INTENT_ENVELOPE_SCHEMA,
        "version": 1,
        "intent": intent.to_dict(),
    }


def approval_id(seed: str | None = None) -> str:
    return "APR-" + (seed or uuid.uuid4().hex[:12]).upper()[:12].rjust(12, "0")


def delivery_gate_evidence(
    action: Mapping[str, Any],
    *,
    decided_at: float,
    expires_at: float,
    approval: str | None = None,
    decision_id: str | None = None,
    action_digest: str | None = None,
) -> dict[str, Any]:
    """Message-delivery-shaped owner approval evidence bound to ``action``."""

    seed = uuid.uuid4().hex[:8]
    return {
        "decision": "approved",
        "authority": "owner",
        "decision_id": decision_id or ("decision-%s" % seed),
        "approval_id": approval or approval_id(),
        "action_id": action["action_id"],
        "action_digest": (
            action["payload_sha256"] if action_digest is None else action_digest
        ),
        "revision": 1,
        "principal": "owner:conformance",
        "channel": "conformance-channel",
        "thread_id": "thread-%s" % seed,
        "event_id": "event-%s" % seed,
        "event_key": "event-key-%s" % seed,
        "delivery_id": "delivery-%s" % seed,
        "delivery_message_id": "message-%s" % seed,
        "binding_method": "delivered_reply",
        "decided_at_epoch": float(decided_at),
        "expires_at_epoch": float(expires_at),
    }


def grant_gate_evidence(
    action: Mapping[str, Any],
    *,
    decided_at: float,
    expires_at: float,
    grant_expires_at: float | str,
    approval: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """Bounded-grant-shaped owner approval evidence bound to ``action``."""

    seed = uuid.uuid4().hex[:8]
    return {
        "decision": "approved",
        "authority": "owner",
        "decision_id": decision_id or ("decision-%s" % seed),
        "approval_id": approval or approval_id(),
        "action_id": action["action_id"],
        "action_digest": action["payload_sha256"],
        "revision": 1,
        "principal": "owner:conformance",
        "bounded_grant_id": "grant-%s" % seed,
        "approval_source_request_id": "request-%s" % seed,
        "bounded_grant_expires_at_epoch": (
            GRANT_UNLIMITED_SENTINEL
            if grant_expires_at == GRANT_UNLIMITED_SENTINEL
            else float(grant_expires_at)
        ),
        "binding_method": GRANT_BINDING_METHOD,
        "decided_at_epoch": float(decided_at),
        "expires_at_epoch": float(expires_at),
    }


__all__ = (
    "HarnessFactory",
    "ManualClock",
    "SqliteStoreHarness",
    "StoreHarness",
    "approval_id",
    "build_envelope",
    "build_intent",
    "delivery_gate_evidence",
    "grant_gate_evidence",
    "sqlite_harness",
)
