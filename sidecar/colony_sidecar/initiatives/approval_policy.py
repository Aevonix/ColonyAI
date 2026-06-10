"""Graduated approval policy helpers (v0.18.0) — outbound target checks.

The owner's policy: nothing waits on a manual approval unless it is
potentially destructive or an outreach to an individual the owner hasn't
authorized yet. The destructive half lives in the action registry's risk
tiers; this module answers the other half — *who* an OUTBOUND action
reaches, and whether that person is an authorized contact.

An OUTBOUND :class:`~colony_sidecar.initiatives.action_registry.ActionSpec`
names the param holding its recipient via ``target_param``. The recipient
value is resolved against the contact store:

- ``gateway:address`` forms (``email:bob@x.com``, ``imessage:+1555...``)
  resolve via ``resolve_handle``;
- ``cid-...`` resolves via ``get``;
- bare emails resolve via ``resolve_handle("email", ...)``;
- anything else falls back to ``find_by_name`` — and only an
  unambiguous match counts (names are not unique; an ambiguous name must
  not authorize the wrong person).

The check fails closed: missing store, missing/unresolvable target, or a
contact with ``interaction_allowed=False`` all deny auto-approval.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from colony_sidecar.contacts.models import GATEWAYS

logger = logging.getLogger(__name__)


def _extract_target(params: Optional[Dict[str, Any]], spec: Any) -> Optional[str]:
    """Pull the recipient value named by ``spec.target_param``.

    Looks in the job params themselves, then the nested ``context`` and
    ``params`` dicts (the autonomy loop carries initiative context under
    ``context``).
    """
    key = getattr(spec, "target_param", None) if spec is not None else None
    if not key or not isinstance(params, dict):
        return None
    containers = (params, params.get("context"), params.get("params"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        value = container.get(key)
        if value:
            return str(value).strip()
    return None


async def _resolve_contact(target: str, contacts_store: Any) -> Optional[Any]:
    """Resolve a recipient identifier to a Contact, or None."""
    # gateway:address form (email:bob@x.com, imessage:+15551234567, ...)
    if ":" in target:
        gateway, _, address = target.partition(":")
        gateway = gateway.strip().lower()
        address = address.strip()
        if gateway in GATEWAYS and address:
            return await contacts_store.resolve_handle(gateway, address)

    # Contact ID — primary key lookup.
    if target.startswith("cid-"):
        return await contacts_store.get(target)

    # Bare email handle.
    if "@" in target:
        return await contacts_store.resolve_handle("email", target)

    # Display name — only when unambiguous (mirrors IdentityResolver).
    matches = await contacts_store.find_by_name(target, threshold=0.9)
    wanted = target.lower()
    exact = [
        m for m in matches
        if (getattr(m, "display_name", "") or "").strip().lower() == wanted
    ]
    candidates = exact or matches
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "Ambiguous outbound target %r: %d contacts match — refusing "
            "to authorize", target, len(candidates),
        )
    return None


async def is_authorized_target(
    params: Optional[Dict[str, Any]],
    spec: Any,
    contacts_store: Any,
) -> Tuple[bool, str]:
    """Is the recipient of an OUTBOUND action an authorized contact?

    Returns ``(True, "contact:<cid>")`` only when the recipient named by
    ``spec.target_param`` resolves to a contact with
    ``interaction_allowed=True``. Everything else fails closed:

    - no contact store wired → ``(False, "no_contact_store")``
    - missing/unresolvable target → ``(False, "unknown_target")``
    - contact found but not authorized → ``(False, "contact_not_authorized")``
    """
    if contacts_store is None:
        return False, "no_contact_store"

    target = _extract_target(params, spec)
    if not target:
        return False, "unknown_target"

    try:
        contact = await _resolve_contact(target, contacts_store)
    except Exception as exc:  # resolution must never break dispatch
        logger.warning("Outbound target resolution failed for %r: %s", target, exc)
        return False, "unknown_target"

    if contact is None:
        return False, "unknown_target"
    if not getattr(contact, "interaction_allowed", False):
        return False, "contact_not_authorized"
    return True, f"contact:{getattr(contact, 'contact_id', 'unknown')}"
