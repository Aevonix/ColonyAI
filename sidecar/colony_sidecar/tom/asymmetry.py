"""Knowledge-asymmetry engine (tom2, H3.2) — daily, default OFF.

Compares the per-contact epistemic rows in SharedFactsStore and derives
second-order inferences:

  * ``knows``      — a contact knows the facts recorded for them;
  * ``unaware_of`` — a contact has no equivalent of a (confident) fact
                     another contact holds.

Fact TEXT is compared in memory only; what persists (live mode) are
Tom2Store rows holding fact IDS — the refs-not-content invariant is the
store's hard write-time pin, this engine simply never has a reason to
violate it.

Modes (COLONY_TOM2): off (default, engine is inert) | shadow (counts only,
writes nothing) | live (writes owner-only inference rows).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# A cross-contact "unaware_of" is an inference, not an observation: born
# modest, and only derived from reasonably confident source facts.
_UNAWARE_CONFIDENCE = 0.4
_MIN_SOURCE_CONFIDENCE = 0.6


def tom2_mode() -> str:
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_TOM2", ("off", "shadow", "live"), "off")


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


class AsymmetryEngine:
    def __init__(self, facts_store: Any, tom2_store: Any,
                 max_facts: int = 500,
                 max_rows_per_contact: int = 50) -> None:
        self._facts = facts_store
        self._tom2 = tom2_store
        self._max_facts = max_facts
        self._max_rows = max_rows_per_contact
        self.last_report: Dict[str, Any] = {}

    def run(self) -> Dict[str, Any]:
        mode = tom2_mode()
        report: Dict[str, Any] = {"mode": mode, "contacts": 0, "knows": 0,
                                  "unaware_of": 0, "written": 0}
        self.last_report = report
        if mode == "off" or self._facts is None or self._tom2 is None:
            return report

        try:
            facts: List[Dict[str, Any]] = self._facts.list_facts(
                limit=self._max_facts).get("facts", [])
        except Exception:
            logger.warning("tom2 asymmetry: facts unavailable", exc_info=True)
            return report

        by_contact: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            cid = str(f.get("contact_id") or "").strip()
            if cid and f.get("id"):
                by_contact.setdefault(cid, []).append(f)
        contacts = sorted(by_contact)
        report["contacts"] = len(contacts)

        for cid in contacts:
            own = by_contact[cid]
            own_texts = {_norm(f.get("fact")) for f in own}

            # First-order grounding: a contact knows their own rows.
            for f in own:
                report["knows"] += 1
                if mode == "live":
                    self._write(cid, "knows", f["id"],
                                float(f.get("confidence") or 0.5), report)

            # Cross-contact asymmetry: confident facts held elsewhere with
            # no textual equivalent in this contact's rows. The text match
            # happens HERE, in memory; only ids are ever written.
            written_rows = 0
            for other in contacts:
                if other == cid:
                    continue
                for f in by_contact[other]:
                    if float(f.get("confidence") or 0.0) < _MIN_SOURCE_CONFIDENCE:
                        continue
                    if _norm(f.get("fact")) in own_texts:
                        continue
                    report["unaware_of"] += 1
                    if mode == "live" and written_rows < self._max_rows:
                        if self._write(cid, "unaware_of", f["id"],
                                       _UNAWARE_CONFIDENCE, report):
                            written_rows += 1

        logger.info(
            "tom2 asymmetry[%s]: contacts=%d knows=%d unaware_of=%d written=%d",
            mode, report["contacts"], report["knows"], report["unaware_of"],
            report["written"])
        return report

    def _write(self, contact_id: str, kind: str, fact_ref: str,
               confidence: float, report: Dict[str, Any]) -> bool:
        try:
            self._tom2.record_inference(
                contact_id=contact_id, kind=kind, fact_ref=str(fact_ref),
                confidence=confidence)
            report["written"] += 1
            return True
        except Exception:
            # A refused write (privacy pin) must never break the sweep.
            logger.debug("tom2 inference write refused", exc_info=True)
            return False
