"""The ActionStore protocol: durable state a governed host worker runs on.

This module is the CONTRACT.  The numbered invariants below are not
descriptions of the reference implementation — they are the obligations any
conforming store must uphold, each one enforced by a named case in
:mod:`colony_hostworker.conformance`.  A store that satisfies the method
signatures but violates an invariant WILL dispatch a second mutation, honor
a dead approval, or resurrect a consumed authorization under crash or
concurrency; the conformance suite exists to catch exactly that before a
host runs live.

Vocabulary
==========
* *action* — one immutable proposed side effect: identity fields plus a
  canonical-JSON payload pinned by ``payload_sha256`` (UTF-8 canonical-JSON
  convention).
* *receipt* — one immutable, append-only evidence record attached to an
  action, addressed by ``(action_id, receipt_key)``, its evidence pinned by
  ``evidence_sha256``.
* *lease* — an exclusive, expiring claim (``owner``, ``lease_expires_at``)
  required for every mutating call.
* *gate* — the receipt of ``kind == "gate"`` holding the owner-approval
  evidence validated by :func:`colony_hostworker.gate.validate_owner_gate`.
* *recovery contract* — the immutable receipt of ``kind ==
  "dispatch_recovery"`` that converts a dispatched action from "ambiguous,
  fail on lease expiry" into "reconcile by bounded GET-only observation".

Lifecycle
=========
``proposed -> gated -> dispatched -> accepted -> verified -> completed``
with ``failed`` reachable from every non-terminal state.  ``dispatched``
NEVER returns to ``gated``: the governed subset pins every action to
``max_attempts == 1``, so once a mutation may have been attempted the only
forward paths are acceptance or explicit ambiguity.  (This is deliberately
stricter than a general-purpose action queue.)

TRANSACTIONAL INVARIANTS — THE CONTRACT
=======================================

I1. IMMUTABLE IDENTITY.  ``action_id``, ``idempotency_key``, ``source``,
    ``source_ref``, ``action_type``, the payload, ``payload_sha256``,
    ``max_attempts``, and ``created_at`` never change after insertion.
    ``get_action`` re-derives the payload digest from the stored payload and
    refuses to return a row whose digest no longer matches.

I2. APPEND-ONLY RECEIPTS.  Receipts are never updated or deleted.  Writing
    ``(action_id, receipt_key)`` again with byte-identical evidence, kind,
    status, and external_id is an idempotent no-op returning the original;
    with anything else it raises :class:`ActionIdempotencyConflict`.

I3. SINGLE-WRITER LEASES.  Every mutating method verifies, inside its own
    transaction, that the caller currently holds an unexpired lease on the
    action; otherwise it raises :class:`ActionLeaseConflict` and writes
    NOTHING.  Lease checks use the store's clock, never the caller's.

I4. IN-TRANSACTION GATE RE-VALIDATION.  ``begin_owner_authorized_dispatch``
    takes the gate validator AS A PARAMETER and invokes it INSIDE the same
    transaction that performs the ``gated -> dispatched`` transition,
    passing the durable action projection, the durable receipt list re-read
    within that transaction, and the store's own clock reading.  A store
    implementation therefore CANNOT forget to re-validate, and the evidence
    validated is what is committed — not what the caller looked at earlier.
    If the validator raises, returns anything but a
    :class:`~colony_hostworker.gate.GateAuthorization`, returns one marked
    ``expired``, or returns one whose ``receipt_key`` / ``approval_id`` /
    ``decision_id`` differ from the caller's expectations, the transaction
    aborts with no state change and no receipt written.

I5. ATOMIC RECOVERY CONTRACT.  The ``gated -> dispatched`` transition and
    the insertion of the immutable GET-only recovery contract (bound to the
    exact ``execution_digest`` about to be PUT) commit in ONE transaction —
    never one without the other.  A second recovery contract for the same
    action is refused.  Together with I4 this is the one-mutation
    guarantee's durable half: any crash after commit leaves an action that
    can only ever be observed, and any crash before commit leaves an action
    whose gate is still unconsumed and whose attempt count is unchanged.

I6. ONE MUTATION, EVER.  ``attempt_count`` increments exactly once, inside
    ``begin_owner_authorized_dispatch``.  A ``dispatched`` action is never
    returned by ``lease_next`` and never transitions back to ``gated``.
    The ONLY way to lease it is ``lease_dispatched_observation``, and only
    while a valid recovery contract with remaining budget exists; a
    dispatched action WITHOUT a valid contract is terminalized as
    explicitly ambiguous (``failed``, dead-lettered) when its lease
    expires — it is never silently retried.

I7. BOUNDED OBSERVATION.  ``begin_dispatched_observation`` durably journals
    observation attempt N (an immutable receipt) BEFORE the caller performs
    any network read, and refuses — terminalizing as ambiguous — once the
    contract's ``max_observations`` or ``observation_deadline`` is reached.
    A corrupt or forged observation journal terminalizes as ambiguous
    rather than granting more attempts.

I8. MONOTONE LIFECYCLE.  Only the transitions in
    :data:`ALLOWED_TRANSITIONS` are possible, checked inside each
    transaction; terminal states are frozen forever.  Every transition
    appends an immutable event record.

I9. VERIFIED MEANS DURABLY WITNESSED.  ``verify`` refuses unless every
    named qualifying receipt already exists on the same action in the same
    store, so an action can never reach ``verified`` (and then
    ``completed``) without its durable acceptance evidence.

I10. FAILURE IS TERMINAL AND EVIDENT.  ``fail_attempt`` moves the action to
    ``failed``, records the error, clears the lease, and dead-letters the
    action in the same transaction.  It never re-opens a dispatched action
    for another mutation regardless of the ``retryable`` argument.

I11. THE STORE'S CLOCK JUDGES TIME.  Expiries, lease deadlines, observation
    deadlines, and the ``now`` handed to the gate validator all come from
    the store's injected clock read inside the transaction.  Caller-
    supplied timestamps are data, never authority.

Error taxonomy
==============
All refusals raise :class:`ActionStoreError` subclasses.  Callers treat any
of them as "no mutation happened here" — which is only true because of
I3/I4/I8 (refusals roll back whole transactions).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .gate import GateAuthorization


class ActionStoreError(RuntimeError):
    """Base class: the store refused an operation and wrote nothing."""


class ActionNotFound(ActionStoreError):
    """The requested durable record does not exist."""


class ActionIdempotencyConflict(ActionStoreError):
    """An idempotency key or receipt key was reused for different content."""


class ActionLeaseConflict(ActionStoreError):
    """The action is not currently leased by the requesting owner."""


class ActionTransitionError(ActionStoreError):
    """The requested lifecycle transition or binding is not permitted."""


STATE_PROPOSED = "proposed"
STATE_GATED = "gated"
STATE_DISPATCHED = "dispatched"
STATE_ACCEPTED = "accepted"
STATE_VERIFIED = "verified"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"

ACTION_STATES = frozenset(
    {
        STATE_PROPOSED,
        STATE_GATED,
        STATE_DISPATCHED,
        STATE_ACCEPTED,
        STATE_VERIFIED,
        STATE_COMPLETED,
        STATE_FAILED,
    }
)

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED})

# NOTE: no dispatched -> gated edge (invariant I6).  The general-purpose
# queue this was extracted from allows that edge for retryable work with a
# remaining attempt budget; the governed subset pins max_attempts == 1 and
# removes the edge entirely so no store bug can re-open a mutation.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_PROPOSED: frozenset({STATE_GATED, STATE_FAILED}),
    STATE_GATED: frozenset({STATE_DISPATCHED, STATE_FAILED}),
    STATE_DISPATCHED: frozenset({STATE_ACCEPTED, STATE_FAILED}),
    STATE_ACCEPTED: frozenset({STATE_VERIFIED, STATE_FAILED}),
    STATE_VERIFIED: frozenset({STATE_COMPLETED, STATE_FAILED}),
    STATE_COMPLETED: frozenset(),
    STATE_FAILED: frozenset(),
}

# Receipt kinds with reserved semantics (I5/I7); stores must not let callers
# forge them through generic receipt insertion paths that skip their checks.
RECOVERY_RECEIPT_KIND = "dispatch_recovery"
OBSERVATION_RECEIPT_KIND = "dispatch_observation"
GATE_RECEIPT_KIND = "gate"

# The in-transaction gate validator (I4): called by the store as
# ``gate_validator(action, receipts, now)`` where ``action`` is the durable
# action projection, ``receipts`` the durable receipt list re-read inside
# the dispatch transaction, and ``now`` the store's clock reading.  It must
# return a non-expired GateAuthorization or raise.
GateValidator = Callable[
    [Mapping[str, Any], Sequence[Mapping[str, Any]], float], GateAuthorization
]


@runtime_checkable
class ActionStore(Protocol):
    """Durable action store contract — see the module docstring invariants.

    Method docstrings state each method's obligations; the invariants I1-I11
    above bind every method.  ``action`` return values are plain mappings
    with at least: ``action_id``, ``idempotency_key``, ``source``,
    ``source_ref``, ``action_type``, ``payload``, ``payload_sha256``,
    ``state``, ``attempt_count``, ``max_attempts``, ``next_attempt_at``,
    ``lease_owner``, ``lease_expires_at``, ``last_error``, ``result``,
    ``created_at``, ``updated_at``, ``terminal_at``.  Receipt mappings carry
    at least: ``action_id``, ``receipt_key``, ``kind``, ``status``,
    ``external_id``, ``evidence``, ``evidence_sha256``, ``observed_at``,
    ``created_at``.
    """

    def recover_expired_leases(
        self,
        *,
        source: str | None = None,
        source_prefix: str | None = None,
        action_type: str | None = None,
        action_ids: Sequence[str] | None = None,
    ) -> None:
        """Reap expired leases within the scope, atomically per action.

        Expired ``gated`` / ``accepted`` / ``verified`` leases are simply
        released (the work is safely repeatable in those states).  An
        expired ``dispatched`` lease is released ONLY when a valid recovery
        contract with remaining observation budget exists; otherwise — or
        when the contract is exhausted or its journal invalid — the action
        is terminalized as explicitly ambiguous (I6/I7).
        """
        ...

    def lease_next(
        self,
        owner: str,
        *,
        lease_seconds: float,
        states: Sequence[str],
        source: str | None = None,
        source_prefix: str | None = None,
        action_type: str | None = None,
        action_ids: Sequence[str] | None = None,
    ) -> Mapping[str, Any] | None:
        """Atomically claim one ready action in ``states`` for ``owner``.

        Only ``gated``, ``accepted``, and ``verified`` are leaseable here —
        NEVER ``dispatched`` (I6).  A row is ready when ``next_attempt_at``
        has passed and no live lease exists.  Returns ``None`` when nothing
        is claimable.
        """
        ...

    def lease_dispatched_observation(
        self,
        owner: str,
        *,
        lease_seconds: float,
        source: str | None = None,
        source_prefix: str | None = None,
        action_type: str | None = None,
        action_ids: Sequence[str] | None = None,
    ) -> Mapping[str, Any] | None:
        """Claim one dispatched action carrying a live recovery contract.

        The ONLY way a dispatched action becomes leaseable, and only for
        GET-only reconciliation (I6).  Dispatched rows whose contract is
        exhausted or invalid are terminalized as ambiguous instead of being
        returned.  Never returns an action to ``gated``.
        """
        ...

    def begin_owner_authorized_dispatch(
        self,
        action_id: str,
        owner: str,
        *,
        gate_receipt_key: str,
        expected_source: str,
        expected_source_ref: str,
        expected_action_type: str,
        expected_payload: Any,
        expected_approval_id: str,
        expected_decision_id: str,
        expected_execution_digest: str,
        observation_window_seconds: float,
        max_observations: int,
        gate_validator: GateValidator,
    ) -> Mapping[str, Any]:
        """Atomically consume one still-live owner gate and open the one
        mutation attempt.

        In ONE transaction (I4 + I5), the store must:

        1. verify the caller's live lease (I3) and ``state == gated`` with
           ``next_attempt_at`` elapsed;
        2. verify the durable row matches EVERY ``expected_*`` binding —
           source, source_ref, action_type, byte-identical canonical
           payload, and payload digest — so the caller authorized what is
           actually stored, not what it remembered;
        3. verify exactly one ``gate`` receipt exists and its
           ``receipt_key`` equals ``gate_receipt_key``;
        4. re-read the durable receipts and CALL ``gate_validator(action,
           receipts, now)`` with the store's clock; require a non-expired
           :class:`~colony_hostworker.gate.GateAuthorization` whose
           ``receipt_key``, ``approval_id``, and ``decision_id`` equal the
           expected values (the store additionally re-checks the structural
           gate bindings itself — two layers, both inside the transaction);
        5. insert the immutable GET-only recovery contract bound to
           ``expected_execution_digest`` with the given observation window
           and budget, refusing if one already exists;
        6. increment ``attempt_count`` (0 -> 1) and transition
           ``gated -> dispatched``.

        Any failure anywhere aborts the whole transaction: no transition,
        no receipt, no attempt consumed.  On return, the caller holds the
        one permission that will ever exist to PUT this execution request.
        """
        ...

    def begin_dispatched_observation(
        self, action_id: str, owner: str, execution_digest: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        """Durably consume one bounded GET attempt BEFORE observing (I7).

        Requires a live lease, ``state == dispatched``, and a valid
        recovery contract matching ``execution_digest``.  Journals attempt
        N as an immutable receipt and returns ``(action, receipt)``.  When
        the budget or deadline is exhausted — or the journal is invalid —
        terminalizes as ambiguous and returns ``(action, None)``.
        """
        ...

    def defer_dispatched_observation(
        self,
        action_id: str,
        owner: str,
        execution_digest: str,
        observation_receipt_key: str,
        reason: str,
        delay_seconds: float,
    ) -> Mapping[str, Any]:
        """Record an unresolved GET and either defer or end as ambiguous.

        ``observation_receipt_key`` must name the journal's CURRENT (last)
        attempt — a stale caller cannot defer over a newer attempt.  If the
        contract still has budget, releases the lease with
        ``next_attempt_at = now + delay_seconds``; otherwise terminalizes
        as ambiguous (I7).  Never re-dispatches.
        """
        ...

    def accept(
        self,
        action_id: str,
        owner: str,
        receipt_key: str,
        kind: str,
        evidence: Any,
        *,
        external_id: str | None = None,
        result: Any = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Record the endpoint's completed projection and move
        ``dispatched -> accepted`` atomically with the acceptance receipt
        (I2/I8).  Requires a live lease."""
        ...

    def verify(
        self,
        action_id: str,
        owner: str,
        receipt_key: str,
        evidence: Any,
        *,
        qualifying_receipt_keys: Sequence[str],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Move ``accepted -> verified`` only if every qualifying receipt
        already exists durably on this action (I9).  Requires a live
        lease."""
        ...

    def complete(
        self, action_id: str, owner: str, result: Any
    ) -> Mapping[str, Any]:
        """Move ``verified -> completed`` with the terminal result, clearing
        the lease (I8).  Requires a live lease."""
        ...

    def fail_attempt(
        self, action_id: str, owner: str, error: str, retryable: bool
    ) -> Mapping[str, Any]:
        """Terminalize as ``failed`` with the error, dead-lettering in the
        same transaction (I10).  ``retryable`` is recorded but NEVER
        re-opens a dispatched action.  Requires a live lease."""
        ...

    def defer_leased(
        self,
        action_id: str,
        owner: str,
        reason: str,
        delay_seconds: float,
        *,
        event_type: str = "deferred",
    ) -> Mapping[str, Any]:
        """Release the lease without changing state or attempt count, for
        work safe to repeat in its current state (``gated`` before the gate
        is consumed, ``accepted``/``verified`` read-only verification).
        Refuses for ``dispatched`` — that path is
        :meth:`defer_dispatched_observation` (I6)."""
        ...

    def list_receipts(self, action_id: str) -> Sequence[Mapping[str, Any]]:
        """Return all receipts for the action in insertion order."""
        ...

    def get_action(self, action_id: str) -> Mapping[str, Any]:
        """Return the action, re-verifying its payload digest (I1).  Raises
        :class:`ActionNotFound` if absent."""
        ...


__all__ = (
    "ACTION_STATES",
    "ALLOWED_TRANSITIONS",
    "ActionIdempotencyConflict",
    "ActionLeaseConflict",
    "ActionNotFound",
    "ActionStore",
    "ActionStoreError",
    "ActionTransitionError",
    "GATE_RECEIPT_KIND",
    "GateValidator",
    "OBSERVATION_RECEIPT_KIND",
    "RECOVERY_RECEIPT_KIND",
    "STATE_ACCEPTED",
    "STATE_COMPLETED",
    "STATE_DISPATCHED",
    "STATE_FAILED",
    "STATE_GATED",
    "STATE_PROPOSED",
    "STATE_VERIFIED",
    "TERMINAL_STATES",
)
