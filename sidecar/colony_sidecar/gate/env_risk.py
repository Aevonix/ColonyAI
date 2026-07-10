"""Environment-risk classifier (L1.2) — the auto-ratchet under leveled tom2.

``classify()`` grades ONE conversation, as experienced by ONE reader, into
R0..R3. It is the input that lets the effective-level resolver auto-degrade
cross-contact rendering the moment the room gets bigger, stranger, or less
certain — with no human in the loop.

    R0 owner-private   — census ⊆ {owner}, DM, private gateway.
    R1 trusted-private — DM with the owner; reader tier >= trusted, strongly
                         resolved; no other non-owner participant.
    R2 known-social    — strongly-resolved reader tier >= regular in a DM;
                         OR a group where EVERY participant is strongly
                         resolved and tier >= trusted. Private gateway.
    R3 open/hostile    — the DEFAULT. Any unknown/shadow/peripheral/
                         group_guest participant, any unresolved group
                         member, a public/embodied/unclassified gateway, an
                         unresolved/system/shadow reader, a missing census,
                         a missing owner identity, or ANY classifier error.

Design pins:

* **Monotone.** Rules only RAISE risk: a lower class is granted solely on
  positive, verified evidence; every missing signal leaves the conversation
  at R3.
* **Fail-closed.** Any exception anywhere (stores, env parsing, lookups)
  returns R3 with a ``classifier-error`` reason — never a lower class.
* **Deployment-configured gateways.** Colony ships NO gateway names.
  ``COLONY_ENV_RISK_GATEWAY_CLASS`` (default empty) maps gateway families to
  ``private`` / ``public`` / ``embodied``; an unclassified gateway is treated
  as hostile, so a deployment must explicitly bless its private DM surfaces
  before anything can grade below R3.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from colony_sidecar.channels.presence import STRONG_METHODS
from colony_sidecar.identity.participants import SYSTEM_CONTACT_ID

logger = logging.getLogger(__name__)

R0, R1, R2, R3 = 0, 1, 2, 3

_LABELS = {R0: "R0", R1: "R1", R2: "R2", R3: "R3"}

#: Trust-tier ordering for floor checks. Anything unlisted ranks 0 (hostile).
_TIER_RANK = {
    "inner_circle": 5,
    "trusted": 4,
    "regular": 3,
    "acquaintance": 2,
    "group_guest": 2,
    "peripheral": 1,
    "unknown": 0,
    "silenced": 0,
}

_GATEWAY_CLASSES = ("private", "public", "embodied")


@dataclass
class EnvRisk:
    level: int
    reasons: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return _LABELS.get(self.level, "R3")

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "label": self.label,
                "reasons": list(self.reasons)}


def env_risk_window_hours() -> float:
    """COLONY_ENV_RISK_WINDOW_HOURS (default 48): census recency window."""
    try:
        v = float(os.environ.get("COLONY_ENV_RISK_WINDOW_HOURS", "48"))
        return v if v > 0 else 48.0
    except (TypeError, ValueError):
        return 48.0


def gateway_class(gateway: str) -> str:
    """Deployment-declared class for a gateway family, or '' (unclassified).

    COLONY_ENV_RISK_GATEWAY_CLASS is a comma list of ``gateway:class`` pairs
    (class in private|public|embodied). Malformed entries are ignored — an
    entry that fails to parse simply leaves its gateway unclassified, which
    is the hostile default.
    """
    g = str(gateway or "").strip().lower()
    if not g:
        return ""
    raw = os.environ.get("COLONY_ENV_RISK_GATEWAY_CLASS", "")
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        name, cls = pair.split(":", 1)
        if name.strip().lower() == g:
            cls = cls.strip().lower()
            return cls if cls in _GATEWAY_CLASSES else ""
    return ""


def _tier_rank(tier: Any) -> int:
    return _TIER_RANK.get(str(tier or "").strip().lower(), 0)


async def _contact_tier(contacts_store: Any, contact_id: str) -> Optional[str]:
    """The contact's trust tier, or None when the contact does not resolve."""
    c = await contacts_store.get(contact_id)
    if c is None:
        return None
    return str(getattr(c, "trust_tier", "") or "")


async def classify(conversation_key: str, reader_contact_id: str, *,
                   presence_store: Any, contacts_store: Any,
                   owner_id: Optional[str] = None) -> EnvRisk:
    """Grade one (conversation, reader) pair into R0..R3. Never raises."""
    try:
        return await _classify(conversation_key, reader_contact_id,
                               presence_store=presence_store,
                               contacts_store=contacts_store,
                               owner_id=owner_id)
    except Exception as exc:
        logger.debug("env-risk classification failed (=> R3): %s", exc,
                     exc_info=True)
        return EnvRisk(R3, [f"classifier-error:{type(exc).__name__}"])


async def _classify(conversation_key: str, reader_contact_id: str, *,
                    presence_store: Any, contacts_store: Any,
                    owner_id: Optional[str]) -> EnvRisk:
    reasons: List[str] = []
    conversation_key = str(conversation_key or "").strip()
    reader = str(reader_contact_id or "").strip()

    # Hard preconditions: each missing signal is terminal (R3).
    if not conversation_key:
        return EnvRisk(R3, ["conversation-key-missing"])
    if not reader or reader == SYSTEM_CONTACT_ID:
        return EnvRisk(R3, ["reader-unresolved-or-system"])
    if presence_store is None:
        return EnvRisk(R3, ["presence-store-missing"])
    if contacts_store is None:
        return EnvRisk(R3, ["contacts-store-missing"])

    if owner_id is None:
        from colony_sidecar.identity.resolver import get_owner_contact_id
        owner_id = get_owner_contact_id()
    owner = str(owner_id or "").strip()
    if not owner:
        return EnvRisk(R3, ["owner-identity-unset"])

    gateway = conversation_key.split(":", 1)[0]
    gclass = gateway_class(gateway)
    if gclass != "private":
        return EnvRisk(R3, [f"gateway-class:{gclass or 'unclassified'}"])

    window = env_risk_window_hours()
    census = presence_store.census(conversation_key, window_hours=window)
    by_id = {str(r.get("contact_id") or ""): r for r in census}
    if reader not in by_id:
        # No verified sighting of the reader in this conversation's window —
        # presence recording is either off or the reader never actually
        # spoke here. Missing signal.
        return EnvRisk(R3, ["reader-not-in-census"])

    def _strong(cid: str) -> bool:
        return str(by_id[cid].get("method") or "").lower() in STRONG_METHODS

    if not _strong(reader):
        return EnvRisk(R3, [
            f"reader-resolution-weak:{by_id[reader].get('method') or 'none'}"])

    non_owner = [cid for cid in by_id if cid != owner]
    is_group = (len(non_owner) > 1
                or any(str(r.get("group_id") or "").strip() for r in census))

    # R0: an owner-private room — every participant in the window IS the
    # owner, and the reader being rendered to IS the owner.
    if reader == owner and not is_group and set(by_id) <= {owner}:
        return EnvRisk(R0, ["owner-private"])

    if reader == owner:
        # Owner reading in a room with other people: grade by the room.
        reasons.append("owner-with-company")

    reader_tier = await _contact_tier(contacts_store, reader)
    if reader_tier is None:
        return EnvRisk(R3, ["reader-contact-unresolvable"])

    if not is_group and set(non_owner) <= {reader}:
        # A true DM between the owner and the (strongly-resolved) reader.
        if _tier_rank(reader_tier) >= _TIER_RANK["trusted"]:
            return EnvRisk(R1, ["trusted-private-dm"])
        if _tier_rank(reader_tier) >= _TIER_RANK["regular"]:
            return EnvRisk(R2, ["known-social-dm"])
        return EnvRisk(R3, [f"reader-tier-below-regular:{reader_tier}"])

    if is_group:
        # R2 group: EVERY participant strongly resolved and tier >= trusted
        # (the owner is exempt from the tier floor, not from resolution).
        for cid in by_id:
            if not _strong(cid):
                return EnvRisk(R3, [f"group-member-unresolved:{cid}"])
            if cid == owner:
                continue
            tier = await _contact_tier(contacts_store, cid)
            if tier is None:
                return EnvRisk(R3, [f"group-member-unresolvable:{cid}"])
            if _tier_rank(tier) < _TIER_RANK["trusted"]:
                return EnvRisk(R3, [f"group-member-below-trusted:{cid}"])
        return EnvRisk(R2, ["known-social-group"])

    # Not a group, but the non-owner set is not just the reader (e.g. a
    # third party was sighted in the window): hostile.
    others = sorted(set(non_owner) - {reader})
    reasons.append("unexpected-co-presence:" + ",".join(others[:5]))
    return EnvRisk(R3, reasons)
