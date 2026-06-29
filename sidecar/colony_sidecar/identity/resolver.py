"""IdentityResolver — normalize person identifiers across Colony subsystems.

A person is referenced by different identifier formats depending on the
subsystem:

- Contact store: CID (``cid-<ts>-<rand>``), display name, platform handles
- Neo4j graph: Person node ID (UUID), ``Person.name``
- Affect store: ``contact_id`` (whatever the caller passed — historically
  display names or CIDs)

The contact store is the source of truth (Step 0 finding, v0.16.0 spec):
a Contact record carries the CID, names, the linked Neo4j node via
``person_node_id``, and all platform handles via the ``contact_handles``
table. The resolver is an index over it.

Owner identity rules:
- The owner is resolved once from ``COLONY_OWNER_CONTACT_ID`` and cached.
- ``COLONY_HOST_CONTACT_ID`` is accepted as a deprecated alias.
- A configured-but-unresolvable owner raises :class:`OwnerIdentityError`.
  There is NO fallback to a default string — the silent ``"owner"``
  default was the bug this module replaces. Callers that filter the owner
  out of generated work must fail closed (generate nothing) rather than
  fall through.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")

from colony_sidecar.channels.phone_gateways import get_phone_gateways as _get_phone_gateways


class OwnerIdentityError(RuntimeError):
    """The owner's identity is missing or cannot be resolved."""


def get_owner_contact_id() -> Optional[str]:
    """Return the configured owner contact ID, honouring the legacy alias.

    ``COLONY_OWNER_CONTACT_ID`` is canonical. ``COLONY_HOST_CONTACT_ID``
    is accepted with a deprecation warning so running deployments keep
    their config. Returns None when neither is set — callers must treat
    that as "owner unknown", never as a default string.
    """
    owner = os.environ.get("COLONY_OWNER_CONTACT_ID")
    if owner:
        return owner
    legacy = os.environ.get("COLONY_HOST_CONTACT_ID")
    if legacy:
        logger.warning(
            "COLONY_HOST_CONTACT_ID is deprecated, use COLONY_OWNER_CONTACT_ID"
        )
        return legacy
    return None


class IdentityResolver:
    """Resolve any identifier for a person to the set of all known forms.

    Backed by the contact store (async API). When the contact store is
    unavailable the resolver degrades to exact-match on the configured
    value only — it never invents identifiers.
    """

    def __init__(
        self,
        contact_store: Any = None,
        owner_id: Optional[str] = None,
    ) -> None:
        self._contact_store = contact_store
        # Capture explicitly so tests can construct without env mutation.
        self._owner_id = owner_id if owner_id is not None else get_owner_contact_id()
        self._owner_set: Optional[frozenset] = None
        # Small bounded cache of is_owner verdicts (one tick scans ~100
        # contacts; name resolution is a full-table similarity scan).
        self._is_owner_cache: dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(self, any_id: str) -> Set[str]:
        """Given any identifier for a person, return all known forms.

        Returns an empty set when the identifier is unknown or ambiguous
        (display names are not unique — an ambiguous name must not merge
        two people into one).
        """
        if not any_id or not isinstance(any_id, str):
            return set()
        contact = await self._lookup(any_id.strip())
        if contact is None:
            return set()
        return await self._identity_set(contact)

    async def is_owner(self, any_id: Any) -> bool:
        """True if ``any_id`` resolves into the owner's identity set.

        Raises :class:`OwnerIdentityError` when the owner identity itself
        cannot be established (missing config, or config that resolves to
        nothing). Callers in generation paths must fail closed on that.
        """
        if not any_id:
            return False
        owners = await self.owner_identities()
        key = str(any_id).strip()
        cached = self._is_owner_cache.get(key)
        if cached is not None:
            return cached

        verdict = key in owners or key.lower() in owners
        if not verdict:
            # Cross-format check: resolve the candidate and intersect.
            try:
                forms = await self.resolve(key)
            except Exception as exc:  # resolution must never break filtering
                logger.warning("Identity resolution failed for %r: %s", key, exc)
                forms = set()
            verdict = bool(forms & owners)

        if len(self._is_owner_cache) > 512:
            self._is_owner_cache.clear()
        self._is_owner_cache[key] = verdict
        return verdict

    async def owner_identities(self) -> frozenset:
        """Resolve and cache the owner's full identity set.

        Raises :class:`OwnerIdentityError` if no owner is configured, or if
        the configured value resolves to nothing in the contact store.
        """
        if self._owner_set is not None:
            return self._owner_set

        owner_id = self._owner_id
        if not owner_id:
            raise OwnerIdentityError(
                "COLONY_OWNER_CONTACT_ID is not set. Owner-exclusion filters "
                "cannot run; set it to the owner's contact CID, Neo4j Person "
                "ID, or an unambiguous display name."
            )

        if self._contact_store is None:
            # No identity index available — exact-match on the configured
            # value (plus case folding). This is the operator's own value,
            # not a made-up default, so it is safe to use directly.
            logger.warning(
                "Contact store unavailable — owner exclusion limited to "
                "exact matches on %r", owner_id,
            )
            self._owner_set = frozenset({owner_id, owner_id.lower()})
            return self._owner_set

        forms = await self.resolve(owner_id)
        if not forms:
            raise OwnerIdentityError(
                f"COLONY_OWNER_CONTACT_ID={owner_id!r} does not resolve to "
                "any contact. Fix the config — refusing to guess."
            )
        # Always include the configured literal so equality checks against
        # raw config values keep working.
        forms |= {owner_id, owner_id.lower()}
        self._owner_set = frozenset(forms)
        logger.info(
            "Owner identity resolved: %d identifier forms cached", len(forms)
        )
        return self._owner_set

    def invalidate(self) -> None:
        """Drop cached owner identity and verdicts (e.g. after a contact merge)."""
        self._owner_set = None
        self._is_owner_cache.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _lookup(self, any_id: str) -> Any:
        """Find the Contact record for an identifier of any format."""
        store = self._contact_store
        if store is None:
            return None

        # CID — primary key lookup.
        if any_id.startswith("cid-"):
            return await store.get(any_id)

        # Email handle.
        if "@" in any_id:
            return await store.resolve_handle("email", any_id)

        # Phone-like handle.
        if _PHONE_RE.match(any_id):
            for gateway in _get_phone_gateways():
                contact = await store.resolve_handle(gateway, any_id)
                if contact is not None:
                    return contact
            return None

        # Neo4j Person node ID (UUID) — forward link lives on the contact.
        contact = await store.find_by_person_node_id(any_id)
        if contact is not None:
            return contact

        # Display name — only when unambiguous. Names are not unique.
        try:
            matches = await store.find_by_name(any_id, threshold=0.9)
        except Exception as exc:
            logger.warning("find_by_name failed for %r: %s", any_id, exc)
            return None
        wanted = any_id.lower()
        exact = [
            m for m in matches
            if (m.display_name or "").strip().lower() == wanted
        ]
        candidates = exact or matches
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning(
                "Ambiguous identity %r: %d contacts match — returning no "
                "identity rather than merging people", any_id, len(candidates),
            )
        return None

    async def _identity_set(self, contact: Any) -> Set[str]:
        """All known identifier forms for a resolved contact."""
        forms: Set[str] = {contact.contact_id}

        display_name = getattr(contact, "display_name", None)
        if display_name:
            forms.add(display_name)
        given = getattr(contact, "given_name", None)
        family = getattr(contact, "family_name", None)
        if given and family:
            forms.add(f"{given} {family}")
        person_node_id = getattr(contact, "person_node_id", None)
        if person_node_id:
            forms.add(person_node_id)

        if self._contact_store is not None:
            try:
                handles = await self._contact_store.get_handles(contact.contact_id)
                for handle in handles:
                    address = getattr(handle, "address", None)
                    if address:
                        forms.add(address)
            except Exception as exc:
                logger.debug(
                    "Handle lookup failed for %s: %s", contact.contact_id, exc
                )

        # Case-folded variants for case-insensitive membership checks.
        forms |= {f.lower() for f in list(forms)}
        return forms


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_resolver: Optional[IdentityResolver] = None


def get_identity_resolver(contact_store: Any = None) -> IdentityResolver:
    """Return the process-wide resolver, creating it on first use.

    When ``contact_store`` is not supplied, the contact store registered
    with the host router is used (same late-binding pattern as
    ``SubsystemRegistry``).
    """
    global _resolver
    if contact_store is None:
        try:
            from colony_sidecar.api.routers import host as _host_mod
            contact_store = getattr(_host_mod, "_contacts_store", None)
        except Exception:
            contact_store = None

    if _resolver is None:
        _resolver = IdentityResolver(contact_store=contact_store)
    elif contact_store is not None and _resolver._contact_store is None:
        # The contact store comes up after the resolver in some boot orders.
        _resolver._contact_store = contact_store
        _resolver.invalidate()
    return _resolver


def reset_identity_resolver() -> None:
    """Reset the singleton (tests and reconfiguration)."""
    global _resolver
    _resolver = None
