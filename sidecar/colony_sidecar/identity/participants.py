"""ParticipantResolver -- per-message sender -> contact attribution.

The attribution chokepoint for relationship intelligence (docs/RELATIONSHIPS.md):
every synced turn that carries a ``sender`` resolves to a real contact here,
server-side, regardless of client caching. Unknown people become shadow
contacts so history accrues from first contact; machines resolve to the
reserved ``system`` sentinel and never touch relationship stores.

Resolution ladder (first hit wins):
  1. exact / cross-gateway messaging handle (phones unify across sms/rcs/
     whatsapp/signal/imessage via phone_key; emails normalize)
  2. scoped display-name: the sender's display name uniquely matches ONE
     member of the group scope this turn came from -> attribute to them AND
     file a merge PROPOSAL for the new handle (never silently link)
  3. shadow contact (tier unknown, interaction_allowed=false, provenance
     recorded), when COLONY_IDENTITY_SHADOW_CONTACTS (default true)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Reserved contact id for machine-origin turns. Comms may record it for ops
#: visibility; ToM/interactions/relationship surfaces must always exclude it.
SYSTEM_CONTACT_ID = "system"


def shadow_contacts_enabled() -> bool:
    return os.environ.get(
        "COLONY_IDENTITY_SHADOW_CONTACTS", "true").strip().lower() != "false"


def machine_channel_prefixes() -> tuple:
    raw = os.environ.get("COLONY_IDENTITY_MACHINE_CHANNELS", "cron,api,internal")
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def is_machine_turn(channel_id: str, user_text: str, has_sender: bool) -> bool:
    """A turn is machine-origin when it has no human sender AND either rides
    a machine channel or carries system-origin text markers. A turn WITH a
    resolved human sender is never reclassified (a human talking on an api
    channel is still a human)."""
    if has_sender:
        return False
    ch = (channel_id or "").strip().lower()
    for prefix in machine_channel_prefixes():
        if ch == prefix or ch.startswith(prefix + ":"):
            return True
    try:
        from colony_sidecar.delivery.reachout_policy import is_system_origin
        if is_system_origin(user_text):
            return True
    except ImportError:
        pass
    return False


@dataclass
class Resolution:
    contact_id: Optional[str]
    method: str          # handle | scoped_name | shadow | none
    created: bool = False
    proposal_filed: bool = False


class ParticipantResolver:
    def __init__(self, contacts_store: Any) -> None:
        self._store = contacts_store

    async def resolve(self, *, platform: str, user_id: str,
                      display_name: str = "", group_id: str = "",
                      channel_id: str = "") -> Resolution:
        """Resolve a sender to a contact id (see module doc for the ladder)."""
        platform = (platform or "").strip().lower()
        user_id = (user_id or "").strip()
        if not user_id:
            return Resolution(None, "none")

        # 1. Handle match (exact + cross-gateway phone + email normalization).
        try:
            c = await self._store.resolve_messaging_handle(platform, user_id)
        except Exception:
            logger.debug("participant handle resolve failed", exc_info=True)
            c = None
        if c is not None:
            return Resolution(c.contact_id, "handle")

        # 2. Scoped display-name: unique name match inside this group's scope.
        if display_name and group_id:
            match = await self._scoped_name_match(platform, group_id, display_name)
            if match is not None:
                filed = await self._file_handle_proposal(
                    match, platform, user_id, display_name)
                logger.info(
                    "participant %r attributed to %s via scoped name; handle "
                    "link proposed (%s:%s)", display_name, match, platform,
                    user_id)
                return Resolution(match, "scoped_name", proposal_filed=filed)

        # 3. Shadow contact.
        if not shadow_contacts_enabled():
            return Resolution(None, "none")
        try:
            contact = await self._store.create(
                display_name=display_name or user_id,
                trust_tier="unknown",
                interaction_allowed=False,
                import_source="auto:sender",
                notes=(f"Auto-created from first contact on {channel_id or platform}"
                       + (f" (group {group_id})" if group_id else "")),
            )
            await self._store.add_handle(
                contact.contact_id, platform, user_id,
                is_primary=True, confidence=0.9, source="auto:sender")
            logger.info("Shadow contact %s created for %s:%s (%r)",
                        contact.contact_id, platform, user_id,
                        display_name or "?")
            return Resolution(contact.contact_id, "shadow", created=True)
        except Exception:
            logger.warning("shadow contact creation failed for %s:%s",
                           platform, user_id, exc_info=True)
            return Resolution(None, "none")

    # -- rung 2 helpers ----------------------------------------------------
    async def _scoped_name_match(self, platform: str, group_id: str,
                                 display_name: str) -> Optional[str]:
        """The display name matches exactly ONE member of this group's scope."""
        try:
            scope = await self._store.get_scope(
                platform=platform, external_id=str(group_id))
            if scope is None:
                return None
            members = await self._store.scope_members(scope.scope_id)
        except Exception:
            return None
        hits = []
        want = display_name.strip().lower()
        for m in members or []:
            cid = getattr(m, "contact_id", None) or (
                m.get("contact_id") if isinstance(m, dict) else None)
            if not cid:
                continue
            try:
                c = await self._store.get(cid)
            except Exception:
                continue
            if c is None:
                continue
            names = {str(c.display_name or "").strip().lower(),
                     str(getattr(c, "given_name", "") or "").strip().lower()}
            if want and (want in names
                         or any(n and want.split()[0] == n.split()[0]
                                for n in names if n)):
                hits.append(c.contact_id)
        return hits[0] if len(hits) == 1 else None

    async def _file_handle_proposal(self, contact_id: str, platform: str,
                                    user_id: str, display_name: str) -> bool:
        """Record the probable handle link for owner review: the handle is
        attached at LOW confidence + unverified (audit-trailed), so the
        attribution sticks while the owner can still correct it."""
        try:
            await self._store.add_handle(
                contact_id, platform, user_id,
                is_primary=False, confidence=0.6, source="auto:scoped-name")
            await self._store.record_audit(
                contact_id, action="handle_proposed",
                detail={"gateway": platform, "address": user_id,
                        "via": "scoped display-name",
                        "display_name": display_name,
                        "note": "verify or remove"},
                performed_by="participant-resolver")
            return True
        except Exception:
            logger.debug("handle proposal failed", exc_info=True)
            return False
