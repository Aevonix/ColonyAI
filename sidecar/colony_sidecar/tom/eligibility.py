"""Level-2 eligibility pipeline (L2.1) — ordered, first-fail-wins.

Decides whether ONE tom2 inference may be considered for level-2 rendering
to ONE reader in ONE conversation. Every check must pass, in order; the
first failure names itself (owner observability) and ends the evaluation.
Any internal error is a failure — never an exemption.

Order (each check exists for a specific threat):

    1. subject-scope       — the subject is a real, resolvable contact and
                             not the reader themself.
    2. subject-owner       — the subject is never the owner (T9: "the owner
                             hasn't heard X" must not be narrated to anyone).
    3. subject-present     — the subject was NOT sighted in this conversation
                             window (T5: never model someone into the room
                             they are standing in).
    4. ref-visibility      — the H3.5 double gate, delegated VERBATIM to
                             tom.tom2.render_inference_for_contact: every ref
                             must resolve to a fact row owned by the reader.
                             This is the structural guarantee that level 2
                             can never introduce new fact content.
    5. mutual-knowledge    — M1: reader and subject demonstrably know of each
                             other (a shared conversation sighting within
                             COLONY_TOM2_MUTUAL_WINDOW_DAYS, default 30).
    6. tier-floors         — M2: reader tier >= trusted, subject tier >=
                             regular.
    7. approval            — M3: the owner has approved this (reader,
                             subject) pair (injected hook; required unless
                             COLONY_TOM2_L2_APPROVAL=off).
    8. budget              — M7: the exposure ledger has budget for this
                             rendering (injected hook; no hook = no budget).

This module is dark: nothing live calls it until the wiring tranche.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from colony_sidecar.gate.env_risk import _tier_rank, env_risk_window_hours
from colony_sidecar.identity.participants import SYSTEM_CONTACT_ID
from colony_sidecar.tom.tom2 import render_inference_for_contact

logger = logging.getLogger(__name__)

CHECKS = ("subject-scope", "subject-owner", "subject-present",
          "ref-visibility", "mutual-knowledge", "tier-floors",
          "approval", "budget")

#: Tier floors (M2): who may RECEIVE a level-2 line, who may be its SUBJECT.
READER_FLOOR = "trusted"
SUBJECT_FLOOR = "regular"

HookResult = Union[bool, Awaitable[bool]]


def l2_approval_mode() -> str:
    """COLONY_TOM2_L2_APPROVAL: 'required' (default) or 'off'. Any other
    value reads as 'required' (fail closed)."""
    raw = os.environ.get("COLONY_TOM2_L2_APPROVAL", "required").strip().lower()
    return "off" if raw == "off" else "required"


def mutual_window_days() -> float:
    """COLONY_TOM2_MUTUAL_WINDOW_DAYS (default 30): how recent the reader/
    subject co-sighting must be to count as mutual knowledge."""
    try:
        v = float(os.environ.get("COLONY_TOM2_MUTUAL_WINDOW_DAYS", "30"))
        return v if v > 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


@dataclass
class EligibilityDecision:
    eligible: bool
    failed_check: Optional[str] = None
    detail: str = ""
    checks_passed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"eligible": self.eligible, "failed_check": self.failed_check,
                "detail": self.detail,
                "checks_passed": list(self.checks_passed)}


def _fail(check: str, detail: str,
          passed: List[str]) -> EligibilityDecision:
    return EligibilityDecision(eligible=False, failed_check=check,
                               detail=detail, checks_passed=list(passed))


async def _hook_ok(hook: Any, *args: Any) -> bool:
    """Run an injected hook; anything but a clean truthy answer is False."""
    if hook is None:
        return False
    try:
        r = hook(*args)
        if hasattr(r, "__await__"):
            r = await r
        return bool(r)
    except Exception:
        logger.debug("eligibility hook failed (=> ineligible)", exc_info=True)
        return False


async def evaluate_inference(
    inference: Dict[str, Any],
    *,
    reader_contact_id: str,
    conversation_key: str,
    facts_store: Any,
    contacts_store: Any,
    presence_store: Any,
    owner_id: Optional[str] = None,
    approval_check: Optional[Callable[[str, str], HookResult]] = None,
    budget_check: Optional[Callable[[str, str, str], HookResult]] = None,
) -> EligibilityDecision:
    """Run the full ordered pipeline for one inference. Never raises."""
    try:
        return await _evaluate(inference,
                               reader_contact_id=reader_contact_id,
                               conversation_key=conversation_key,
                               facts_store=facts_store,
                               contacts_store=contacts_store,
                               presence_store=presence_store,
                               owner_id=owner_id,
                               approval_check=approval_check,
                               budget_check=budget_check)
    except Exception as exc:
        logger.debug("eligibility evaluation failed (=> ineligible): %s",
                     exc, exc_info=True)
        return _fail("error", f"{type(exc).__name__}", [])


async def _evaluate(inference, *, reader_contact_id, conversation_key,
                    facts_store, contacts_store, presence_store, owner_id,
                    approval_check, budget_check) -> EligibilityDecision:
    passed: List[str] = []
    reader = str(reader_contact_id or "").strip()
    subject = str((inference or {}).get("contact_id") or "").strip()

    # 1. subject-scope
    if not isinstance(inference, dict) or not subject:
        return _fail("subject-scope", "no subject", passed)
    if not reader or reader == SYSTEM_CONTACT_ID:
        return _fail("subject-scope", "no reader", passed)
    if subject == reader or subject == SYSTEM_CONTACT_ID:
        return _fail("subject-scope", "subject is reader/system", passed)
    if contacts_store is None:
        return _fail("subject-scope", "contacts store missing", passed)
    subject_contact = await contacts_store.get(subject)
    if subject_contact is None:
        return _fail("subject-scope", "subject unresolvable", passed)
    passed.append("subject-scope")

    # 2. subject-owner (T9). Owner identity unknown fails closed: we cannot
    # PROVE the subject is not the owner.
    if owner_id is None:
        from colony_sidecar.identity.resolver import get_owner_contact_id
        owner_id = get_owner_contact_id()
    owner = str(owner_id or "").strip()
    if not owner:
        return _fail("subject-owner", "owner identity unset", passed)
    if subject == owner:
        return _fail("subject-owner", "subject is the owner", passed)
    passed.append("subject-owner")

    # 3. subject-present (T5). A broken/missing presence store cannot prove
    # absence — fail closed.
    if presence_store is None:
        return _fail("subject-present", "presence store missing", passed)
    try:
        present = presence_store.is_present(
            conversation_key, subject, window_hours=env_risk_window_hours())
    except Exception:
        return _fail("subject-present", "presence read failed", passed)
    if present:
        return _fail("subject-present", "subject sighted in conversation",
                     passed)
    passed.append("subject-present")

    # 4. ref-visibility — the H3.5 gate, verbatim. None = ineligible,
    # whether from partial visibility, a missing fact, or the master
    # COLONY_TOM2_CROSS_CONTEXT flag being off.
    if render_inference_for_contact(inference, facts_store, reader) is None:
        return _fail("ref-visibility", "H3.5 gate refused", passed)
    passed.append("ref-visibility")

    # 5. mutual-knowledge (M1)
    try:
        mutual = presence_store.cooccurred(reader, subject,
                                           within_days=mutual_window_days())
    except Exception:
        return _fail("mutual-knowledge", "cooccurrence read failed", passed)
    if not mutual:
        return _fail("mutual-knowledge",
                     "no recent shared conversation", passed)
    passed.append("mutual-knowledge")

    # 6. tier-floors (M2)
    reader_contact = await contacts_store.get(reader)
    if reader_contact is None:
        return _fail("tier-floors", "reader unresolvable", passed)
    if _tier_rank(getattr(reader_contact, "trust_tier", "")) < \
            _tier_rank(READER_FLOOR):
        return _fail("tier-floors", "reader below trusted", passed)
    if _tier_rank(getattr(subject_contact, "trust_tier", "")) < \
            _tier_rank(SUBJECT_FLOOR):
        return _fail("tier-floors", "subject below regular", passed)
    passed.append("tier-floors")

    # 7. approval (M3)
    if l2_approval_mode() == "off":
        passed.append("approval")
    elif await _hook_ok(approval_check, reader, subject):
        passed.append("approval")
    else:
        return _fail("approval", "pair not approved by owner", passed)

    # 8. budget (M7): no ledger, no budget.
    fact_ref = str((inference or {}).get("fact_ref") or "")
    if not await _hook_ok(budget_check, reader, subject, fact_ref):
        return _fail("budget", "no exposure budget", passed)
    passed.append("budget")

    return EligibilityDecision(eligible=True, checks_passed=passed)


async def eligible_inferences(rows: List[Dict[str, Any]], *,
                              limit: int = 5, **kwargs: Any
                              ) -> List[Dict[str, Any]]:
    """Filter inference rows through the pipeline (order preserved), up to
    ``limit`` eligible results. Errors inside any evaluation drop that row."""
    out: List[Dict[str, Any]] = []
    limit = max(0, int(limit))
    if limit == 0:
        return out
    for row in rows or []:
        decision = await evaluate_inference(row, **kwargs)
        if decision.eligible:
            out.append(row)
        if len(out) >= limit:
            break
    return out
