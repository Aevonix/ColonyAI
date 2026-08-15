"""Approval-gate invariant core and provenance-shape registry.

An owner approval gate is a durable receipt whose ``evidence`` mapping must be
EXACTLY one registered authorization shape — never a subset, superset, or mix.
Two shapes are built in:

* message-delivery provenance — a per-message owner approval bound to one
  delivered approval request (``channel``, ``thread_id``, ``event_id``,
  ``event_key``, ``delivery_id``, ``delivery_message_id``,
  ``binding_method``);
* bounded-grant provenance — an owner-issued grant consumed at
  approval time (``bounded_grant_id``, ``approval_source_request_id``,
  ``bounded_grant_expires_at_epoch``, ``binding_method`` exactly
  ``"bounded_grant"``). The grant expiry is either a finite epoch or the
  explicit literal ``"unlimited"``; every per-action gate expiry stays finite.

Invariants enforced here, in one place, for every shape:

* exact field-set equality against exactly one registered shape;
* ``decision == "approved"``, ``authority == "owner"``, receipt
  ``status == "passed"``;
* ``action_id`` equality and ``action_digest`` bound to the action's
  ``payload_sha256`` via :func:`hmac.compare_digest`;
* ``revision`` pinned to exactly 1;
* receipt ``external_id`` equal to the evidence ``approval_id``;
* ``evidence_sha256`` recomputed (UTF-8 canonical JSON) and compared;
* timestamp sanity with the contract's 30-second skew allowance; and
* EVERY expiry the shape carries (the gate expiry AND any grant expiry)
  re-enforced at point of use — an expired authority never dispatches, no
  matter what was true when the receipt was written.

The grant path additionally fails closed for any tool the catalog marks
``non_grantable`` (and for any tool the catalog does not know), regardless of
host configuration: no registry, shape, or configuration input to this module
can authorize ``colony_autonomy_enable`` / ``colony_autonomy_disable`` on a
standing grant.
"""

from __future__ import annotations

import hmac
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .catalog import TOOL_CATALOG, ToolSpec
from .contract import (
    APPROVAL_ID_RE,
    GATE_CLOCK_SKEW_SECONDS,
    GovernedContractError,
    sha256_json_utf8,
)


class OwnerGateError(GovernedContractError):
    """The owner approval gate does not bind this action."""


class ProvenanceShapeError(GovernedContractError):
    """A provenance shape registration is invalid or ambiguous."""


# Fields common to every authorization shape.  A shape adds only provenance.
GATE_COMMON_FIELDS = frozenset(
    {
        "decision",
        "authority",
        "decision_id",
        "approval_id",
        "action_id",
        "action_digest",
        "revision",
        "principal",
        "decided_at_epoch",
        "expires_at_epoch",
    }
)

# ``binding_method`` value reserved exclusively for the bounded-grant shape.
GRANT_BINDING_METHOD = "bounded_grant"
GRANT_UNLIMITED_SENTINEL = "unlimited"

_GATE_STRING_MAX = 512


@dataclass(frozen=True, slots=True)
class ProvenanceShape:
    """One exact evidence field-set an owner approval gate may take.

    ``provenance_fields`` always includes ``binding_method``.  A shape either
    pins ``binding_method`` to one exact reserved value (the grant shape pins
    ``"bounded_grant"``) or accepts any bounded non-empty string that is not
    reserved by another registered shape.  ``expiry_fields`` are provenance
    epoch fields re-enforced at point of use in addition to the common gate
    expiry.
    """

    name: str
    provenance_fields: frozenset[str]
    grants: bool = False
    binding_method: str | None = None
    expiry_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ProvenanceShapeError("provenance shape name is invalid")
        if "binding_method" not in self.provenance_fields:
            raise ProvenanceShapeError(
                "provenance shape must carry binding_method"
            )
        if self.provenance_fields & GATE_COMMON_FIELDS:
            raise ProvenanceShapeError(
                "provenance fields may not shadow common gate fields"
            )
        if any(name not in self.provenance_fields for name in self.expiry_fields):
            raise ProvenanceShapeError(
                "expiry fields must be provenance fields"
            )
        if "binding_method" in self.expiry_fields:
            raise ProvenanceShapeError("binding_method is never an expiry")
        if self.grants and self.binding_method != GRANT_BINDING_METHOD:
            raise ProvenanceShapeError(
                "a grant shape must pin binding_method to the reserved "
                "bounded-grant value"
            )
        if not self.grants and self.binding_method == GRANT_BINDING_METHOD:
            raise ProvenanceShapeError(
                "the bounded-grant binding method is reserved for the "
                "grant shape only"
            )

    @property
    def evidence_fields(self) -> frozenset[str]:
        return GATE_COMMON_FIELDS | self.provenance_fields

    @property
    def string_fields(self) -> tuple[str, ...]:
        """Provenance fields checked as bounded non-empty strings."""

        return tuple(
            sorted(self.provenance_fields - set(self.expiry_fields))
        )


MESSAGE_DELIVERY_SHAPE = ProvenanceShape(
    name="message_delivery",
    provenance_fields=frozenset(
        {
            "channel",
            "thread_id",
            "event_id",
            "event_key",
            "delivery_id",
            "delivery_message_id",
            "binding_method",
        }
    ),
    grants=False,
    binding_method=None,
    expiry_fields=(),
)

BOUNDED_GRANT_SHAPE = ProvenanceShape(
    name="bounded_grant",
    provenance_fields=frozenset(
        {
            "bounded_grant_id",
            "approval_source_request_id",
            "bounded_grant_expires_at_epoch",
            "binding_method",
        }
    ),
    grants=True,
    binding_method=GRANT_BINDING_METHOD,
    expiry_fields=("bounded_grant_expires_at_epoch",),
)


class ProvenanceShapeRegistry:
    """Exact-match shape registry that refuses every ambiguous registration.

    Selection is by EXACT evidence field-set equality only — never subset or
    superset matching.  Registration refuses shapes whose field sets are
    equal to, a subset of, or a superset of an already-registered shape
    (either would invite subset-matching mistakes later), refuses a second
    claim on a pinned ``binding_method`` value, and reserves
    ``"bounded_grant"`` for the grant shape only.
    """

    def __init__(self, shapes: Iterable[ProvenanceShape] = ()) -> None:
        self._shapes: dict[str, ProvenanceShape] = {}
        for shape in shapes:
            self.register(shape)

    def register(self, shape: ProvenanceShape) -> None:
        if not isinstance(shape, ProvenanceShape):
            raise ProvenanceShapeError("provenance shape is invalid")
        if shape.name in self._shapes:
            raise ProvenanceShapeError(
                "provenance shape name is already registered"
            )
        for existing in self._shapes.values():
            if (
                shape.evidence_fields == existing.evidence_fields
                or shape.evidence_fields <= existing.evidence_fields
                or shape.evidence_fields >= existing.evidence_fields
            ):
                raise ProvenanceShapeError(
                    "provenance shape overlaps a registered shape"
                )
            if (
                shape.binding_method is not None
                and shape.binding_method == existing.binding_method
            ):
                raise ProvenanceShapeError(
                    "binding_method value is already reserved"
                )
        self._shapes[shape.name] = shape

    @property
    def shapes(self) -> tuple[ProvenanceShape, ...]:
        return tuple(self._shapes.values())

    def reserved_binding_methods(self) -> frozenset[str]:
        return frozenset(
            shape.binding_method
            for shape in self._shapes.values()
            if shape.binding_method is not None
        )

    def select(self, evidence_fields: Iterable[str]) -> ProvenanceShape:
        """Return the one shape whose evidence field set matches EXACTLY."""

        fields = frozenset(evidence_fields)
        for shape in self._shapes.values():
            if fields == shape.evidence_fields:
                return shape
        raise OwnerGateError("owner approval gate is invalid")

    def binding_method_valid(self, shape: ProvenanceShape, value: Any) -> bool:
        """True iff ``value`` is a binding method this shape may carry.

        A pinned shape requires its exact value.  An open shape accepts any
        value except those reserved by other shapes — so a delivery-shaped
        receipt claiming ``"bounded_grant"`` (or any other reserved value) is
        refused even though its field set matched.
        """

        if not isinstance(value, str) or not value:
            return False
        if shape.binding_method is not None:
            return value == shape.binding_method
        return value not in self.reserved_binding_methods()


def default_registry() -> ProvenanceShapeRegistry:
    return ProvenanceShapeRegistry((MESSAGE_DELIVERY_SHAPE, BOUNDED_GRANT_SHAPE))


DEFAULT_REGISTRY = default_registry()


@dataclass(frozen=True, slots=True)
class GateAuthorization:
    """The one bounded projection of a passing owner approval gate."""

    shape: str
    granted: bool
    receipt_key: str
    evidence_sha256: str
    approval_id: str
    decision_id: str
    revision: int
    decided_at: float
    expires_at: float
    expired: bool


def _epoch(evidence: Mapping[str, Any], field_name: str) -> float:
    try:
        return float(evidence.get(field_name))
    except (TypeError, ValueError) as error:
        raise OwnerGateError("owner approval time is invalid") from error


def validate_owner_gate(
    action: Mapping[str, Any],
    receipts: Iterable[Mapping[str, Any]],
    *,
    tool_name: str,
    now: float,
    registry: ProvenanceShapeRegistry = DEFAULT_REGISTRY,
    catalog: Mapping[str, ToolSpec] = TOOL_CATALOG,
) -> GateAuthorization:
    """Validate exactly one still-bindable owner approval gate.

    ``action`` supplies the durable binding targets (``action_id`` and the
    UTF-8 payload digest ``payload_sha256``); ``receipts`` is the action's
    full receipt list; ``now`` is the caller's point-of-use clock reading.
    Raises :class:`OwnerGateError` unless every invariant in the module
    docstring holds.  ``expired`` on the returned authorization is True when
    ANY expiry the shape carries has passed — callers must treat an expired
    authorization as non-dispatchable.
    """

    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise OwnerGateError("owner approval clock is invalid")
    now = float(now)
    if not math.isfinite(now) or now <= 0:
        raise OwnerGateError("owner approval clock is invalid")

    gates = [
        receipt
        for receipt in receipts
        if isinstance(receipt, Mapping) and receipt.get("kind") == "gate"
    ]
    if len(gates) != 1:
        raise OwnerGateError("exactly one owner approval gate is required")
    receipt = gates[0]
    evidence = receipt.get("evidence")
    if not isinstance(evidence, Mapping) or any(
        not isinstance(key, str) for key in evidence
    ):
        raise OwnerGateError("owner approval gate is invalid")

    # Exact field-set match against exactly one registered shape; a partial
    # or mixed shape never selects and is refused here.
    shape = registry.select(evidence)

    # A standing grant never authorizes a tool the catalog marks
    # non-grantable — or a tool the catalog does not know — regardless of
    # what the grant, the registry, or any host configuration says.
    if shape.grants:
        spec = catalog.get(tool_name)
        if spec is None or spec.non_grantable:
            raise OwnerGateError("bounded grant authority never covers this tool")

    decided_at = _epoch(evidence, "decided_at_epoch")
    expires_at = _epoch(evidence, "expires_at_epoch")
    standing_grant = bool(
        shape.grants
        and shape.binding_method == GRANT_BINDING_METHOD
        and evidence.get("bounded_grant_expires_at_epoch")
        == GRANT_UNLIMITED_SENTINEL
    )
    extra_expiries = tuple(
        _epoch(evidence, field_name)
        for field_name in shape.expiry_fields
        if not (
            standing_grant
            and field_name == "bounded_grant_expires_at_epoch"
        )
    )

    receipt_key = receipt.get("receipt_key")
    evidence_sha256 = receipt.get("evidence_sha256")
    if (
        receipt.get("status") != "passed"
        or evidence.get("decision") != "approved"
        or evidence.get("authority") != "owner"
        or evidence.get("action_id") != action.get("action_id")
        or not hmac.compare_digest(
            str(evidence.get("action_digest") or ""),
            str(action.get("payload_sha256") or ""),
        )
        or isinstance(evidence.get("revision"), bool)
        or evidence.get("revision") != 1
        or receipt.get("external_id") != evidence.get("approval_id")
        or not isinstance(receipt_key, str)
        or not receipt_key
        or not isinstance(evidence_sha256, str)
        or not hmac.compare_digest(
            evidence_sha256, sha256_json_utf8(dict(evidence))
        )
        or not APPROVAL_ID_RE.fullmatch(str(evidence.get("approval_id") or ""))
        or any(
            not isinstance(evidence.get(field_name), str)
            or not evidence.get(field_name)
            or len(evidence.get(field_name)) > _GATE_STRING_MAX
            for field_name in ("decision_id", "principal") + shape.string_fields
        )
        or not registry.binding_method_valid(
            shape, evidence.get("binding_method")
        )
        or not math.isfinite(decided_at)
        or not math.isfinite(expires_at)
        or not all(math.isfinite(epoch) for epoch in extra_expiries)
        or decided_at <= 0
        or decided_at > now + GATE_CLOCK_SKEW_SECONDS
        or expires_at <= decided_at
        or any(epoch <= 0 for epoch in extra_expiries)
    ):
        raise OwnerGateError("owner approval gate does not bind this action")

    return GateAuthorization(
        shape=shape.name,
        granted=shape.grants,
        receipt_key=receipt_key,
        evidence_sha256=evidence_sha256,
        approval_id=evidence["approval_id"],
        decision_id=evidence["decision_id"],
        revision=1,
        decided_at=decided_at,
        expires_at=expires_at,
        # Point-of-use re-enforcement of EVERY expiry the shape carries: an
        # authority expired now must never dispatch, regardless of what was
        # true when the receipt was written.
        expired=expires_at <= now
        or any(epoch <= now for epoch in extra_expiries),
    )


def assert_dispatchable(authorization: GateAuthorization) -> GateAuthorization:
    """Refuse an authorization whose expiry has passed at point of use.

    :func:`validate_owner_gate` mirrors the deployed worker exactly and
    reports expiry as data (``expired``); dispatchers MUST route through this
    helper (or an equivalent check) so an expired gate or grant can never
    reach a mutation.
    """

    if not isinstance(authorization, GateAuthorization):
        raise OwnerGateError("owner approval authorization is invalid")
    if authorization.expired:
        raise OwnerGateError(
            "owner approval authority is expired at point of use"
        )
    return authorization


__all__ = (
    "BOUNDED_GRANT_SHAPE",
    "DEFAULT_REGISTRY",
    "GATE_COMMON_FIELDS",
    "GRANT_BINDING_METHOD",
    "GRANT_UNLIMITED_SENTINEL",
    "GateAuthorization",
    "MESSAGE_DELIVERY_SHAPE",
    "OwnerGateError",
    "ProvenanceShape",
    "ProvenanceShapeError",
    "ProvenanceShapeRegistry",
    "assert_dispatchable",
    "default_registry",
    "validate_owner_gate",
)
