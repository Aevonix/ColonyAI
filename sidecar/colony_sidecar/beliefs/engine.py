"""BeliefEngine -- periodic belief maintenance over graph + world model.

Runs from the autonomy loop's daily `_phase_belief_maintenance`:
1. World-model supersessions: snapshot-diff entity properties; a changed
   value writes a supersession audit row (who/what/when/why survives even
   though the store keeps only the winning value).
2. Graph contradictions: conservative claim extraction over recent semantic
   memories; same subject+predicate with conflicting values -> conflict
   record. Resolution (live mode) marks the loser's epistemic_state
   "superseded" with an audit trail; unresolvable conflicts become internal
   review initiatives (never a reach-out).
3. Stale decay: world-model entities unseen past the TTL lose confidence
   (live mode).

Shadow mode (default) is the calibration stage: detect + record + surface,
mutate nothing. Every resolution/decay in live mode is journaled.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from colony_sidecar.beliefs.contradictions import (
    claims_from_text, detect_conflicts, property_claims,
)
from colony_sidecar.beliefs.decay import decay_entity, stale_entities
from colony_sidecar.beliefs.models import Claim, beliefs_mode
from colony_sidecar.beliefs.resolve import pick_winner
from colony_sidecar.beliefs.store import BeliefStore

logger = logging.getLogger(__name__)

_MEMORY_QUERY = """
MATCH (m:Memory)
WHERE m.type IN ['semantic', 'episodic']
  AND m.superseded_by IS NULL
  AND NOT coalesce(m.epistemic_state, 'inferred')
      IN ['superseded', 'deprecated', 'archived']
RETURN m.id AS id, m.content AS content, m.source_type AS source_type,
       coalesce(m.effective_confidence, m.base_confidence, 0.5) AS confidence,
       m.created_at AS created_at
ORDER BY m.created_at DESC
LIMIT $limit
"""


class BeliefEngine:
    def __init__(self, store: BeliefStore, *, world_store: Any = None,
                 graph: Any = None, initiative_store: Any = None,
                 journal: Any = None, self_model: Any = None) -> None:
        self.store = store
        self._world = world_store
        self._graph = graph
        self._initiatives = initiative_store
        self._journal = journal
        self._self_model = self_model
        self.last_report: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Inline hook: cheap detection at property-update time
    # ------------------------------------------------------------------

    def note_property_update(self, entity_id: str, key: str,
                             old_value: Any, new_value: Any,
                             old_confidence: float,
                             new_confidence: float) -> None:
        """Called inline when a world-model property changes value. Records
        the superseded value; heavy resolution stays in the periodic run."""
        from colony_sidecar.beliefs.models import norm_value
        if norm_value(old_value) == norm_value(new_value):
            return
        try:
            self.store.record_supersession(
                "world_model", entity_id, key, old_value, new_value,
                old_confidence, new_confidence,
                reason="higher-confidence update", actor="world_model_store")
        except Exception:
            logger.debug("inline supersession record failed", exc_info=True)

    # ------------------------------------------------------------------
    # Periodic run
    # ------------------------------------------------------------------

    def _effective_mode(self) -> str:
        """Env mode graduated by the trust engine (Amendment 1.2). Belief
        RESOLUTION mutates epistemic state autonomously (no sub-gate), so it
        requires the fully-earned act_first stage; env "live" remains the
        owner override, "off" stays off.

        The trust ladder had a catch-22 for beliefs: graduation ask_first ->
        act_first needs a real (non-shadow) track record, but the engine only
        acted at act_first, so every outcome stayed shadow and the domain
        could never graduate. COLONY_BELIEFS_SUPERVISED_LIVE=1 adds a
        'supervised' rung at ask_first: REVERSIBLE mutations only (epistemic
        supersession that preserves the old value on the superseded node,
        and bounded stale-confidence decay — no deletion), journaled with
        prior state, outcomes recorded shadow=False. Destructive resolution
        (anything that deletes or merges nodes) must still gate on the full
        "live" mode, i.e. act_first or the explicit env override."""
        mode = beliefs_mode()
        if mode in ("off", "live"):
            return mode
        trust = getattr(self._self_model, "trust", None)
        if trust is None:
            return mode
        try:
            stage = trust.stage("beliefs", default="shadow")
        except Exception:
            return mode
        if stage == "act_first":
            return "live"
        from colony_sidecar.beliefs.models import beliefs_supervised_live
        if stage == "ask_first" and beliefs_supervised_live():
            return "supervised"
        return "shadow"

    async def run(self) -> Dict[str, Any]:
        mode = self._effective_mode()
        report: Dict[str, Any] = {
            "mode": mode, "supersessions": 0, "conflicts_detected": 0,
            "resolved": 0, "review_initiatives": 0, "decayed": 0,
        }
        self.last_report = report
        if mode == "off":
            return report
        try:
            await self._world_model_pass(report)
        except Exception:
            logger.debug("belief world-model pass failed", exc_info=True)
        try:
            await self._graph_pass(report, mode)
        except Exception:
            logger.debug("belief graph pass failed", exc_info=True)
        try:
            await self._decay_pass(report, mode)
        except Exception:
            logger.debug("belief decay pass failed", exc_info=True)
        logger.info(
            "belief-maintenance[%s]: supersessions=%d conflicts=%d "
            "resolved=%d review=%d decayed=%d", mode,
            report["supersessions"], report["conflicts_detected"],
            report["resolved"], report["review_initiatives"],
            report["decayed"])
        if self._self_model is not None:
            try:
                # supervised outcomes are REAL (shadow=False): that is the
                # whole point of the rung — a track record the trust engine
                # can graduate ask_first -> act_first on.
                self._self_model.record(
                    "beliefs", "success",
                    shadow=(mode not in ("live", "supervised")))
            except Exception:
                pass
        return report

    # -- world model: snapshot diff -> supersession audit -------------------
    async def _world_model_pass(self, report: Dict[str, Any]) -> None:
        if self._world is None:
            return
        try:
            ents = await self._world.find_entities(query="",
                                                   min_confidence=0.0,
                                                   limit=500)
        except Exception:
            return
        from colony_sidecar.beliefs.models import norm_value
        for e in ents or []:
            for c in property_claims(e):
                snap = self.store.snapshot_get(c.ref, c.predicate)
                if snap is not None and norm_value(snap["value"]) != norm_value(c.value):
                    self.store.record_supersession(
                        "world_model", c.subject, c.predicate,
                        snap["value"], c.value,
                        float(snap.get("confidence") or 0.0), c.confidence,
                        reason="property value changed since last scan",
                        actor="belief_engine")
                    report["supersessions"] += 1
                self.store.snapshot_put(c.ref, c.predicate, c.value,
                                        c.confidence)

    # -- graph: claim conflicts -> resolve / review ---------------------------
    async def _graph_pass(self, report: Dict[str, Any], mode: str) -> None:
        claims = await self._graph_claims()
        if not claims:
            return
        for a, b in detect_conflicts(claims):
            report["conflicts_detected"] += 1
            cid = self.store.record_conflict(
                "graph", a.subject, a.predicate, a.value, b.value,
                meta_a={"ref": a.ref, "source": a.source,
                        "confidence": a.confidence, "ts": a.ts},
                meta_b={"ref": b.ref, "source": b.source,
                        "confidence": b.confidence, "ts": b.ts})
            picked = pick_winner(a, b)
            if picked is None:
                self.store.resolve_conflict(
                    cid, "unresolvable: equal recency/confidence/trust",
                    status="review")
                self._surface_review(a, b, cid)
                report["review_initiatives"] += 1
                continue
            winner, loser = picked
            # Supersession is the REVERSIBLE resolution: the loser node is
            # marked (epistemic_state + superseded_by pointer) with its old
            # value preserved on it, and the transition is journaled — so
            # the supervised rung may perform it. Any future DESTRUCTIVE
            # resolution (deleting or merging nodes) must check
            # mode == "live" here, never "supervised".
            if mode not in ("live", "supervised"):
                logger.info(
                    "SHADOW-BELIEF conflict %s: would supersede %r "
                    "(%s=%s) in favor of %r", cid, loser.value,
                    a.subject, a.predicate, winner.value)
                continue
            await self._supersede_memory(winner, loser, cid)
            report["resolved"] += 1

    async def _graph_claims(self, limit: int = 200) -> List[Claim]:
        if self._graph is None or not hasattr(self._graph, "run_query"):
            return []
        try:
            # Register this exact parameterized read query with the graph
            # client's Cypher allowlist (single-sourced here).
            if hasattr(type(self._graph), "register_allowed_cypher"):
                type(self._graph).register_allowed_cypher(_MEMORY_QUERY)
            rows = await self._graph.run_query(_MEMORY_QUERY,
                                               {"limit": int(limit)})
        except Exception as exc:
            logger.debug("belief memory query failed: %s", exc)
            return []
        claims: List[Claim] = []
        for r in rows or []:
            content = str(r.get("content") or "")
            created = r.get("created_at")
            try:
                ts = created.to_native().timestamp() if hasattr(
                    created, "to_native") else (
                    created.timestamp() if hasattr(created, "timestamp")
                    else 0.0)
            except Exception:
                ts = 0.0
            claims.extend(claims_from_text(
                content,
                confidence=float(r.get("confidence") or 0.5),
                ts=ts, source=str(r.get("source_type") or "inference"),
                ref=str(r.get("id") or "")))
        return claims

    async def _supersede_memory(self, winner: Claim, loser: Claim,
                                conflict_id: str) -> None:
        try:
            if loser.ref and hasattr(self._graph, "transition_epistemic_state"):
                await self._graph.transition_epistemic_state(
                    loser.ref, "superseded",
                    superseded_by=winner.ref or None)
            self.store.record_supersession(
                "graph", loser.subject, loser.predicate, loser.value,
                winner.value, loser.confidence, winner.confidence,
                reason=f"conflict {conflict_id}: lost on "
                       "recency/confidence/trust ordering")
            self.store.resolve_conflict(
                conflict_id,
                f"kept {winner.value!r} ({winner.source}, "
                f"conf={winner.confidence:.2f}); superseded {loser.ref}")
            if self._journal is not None:
                self._journal.record(
                    "beliefs",
                    f"superseded belief {loser.subject} {loser.predicate}="
                    f"{loser.value!r} in favor of {winner.value!r}",
                    reasoning="conflict resolution: recency > confidence > "
                              "source trust",
                    confidence=winner.confidence,
                    reversibility="recoverable", decision="acted",
                    ref=conflict_id)
        except Exception:
            logger.debug("supersede failed for %s", conflict_id,
                         exc_info=True)

    def _surface_review(self, a: Claim, b: Claim, conflict_id: str) -> None:
        """Unresolvable -> internal review initiative (never a reach-out)."""
        if self._initiatives is None:
            return
        try:
            self._initiatives.create(
                type="data_quality",
                description=(
                    f"Belief conflict needs review: {a.subject} "
                    f"{a.predicate} = {a.value!r} vs {b.value!r} "
                    f"(equal recency/confidence/trust)"),
                priority=0.55,
                rationale=f"belief conflict {conflict_id}; sources "
                          f"{a.source} vs {b.source}",
                dedup_key=f"belief_conflict:{conflict_id}",
                source_type="belief_maintenance",
                created_by="belief_engine",
            )
        except Exception:
            logger.debug("review initiative creation failed", exc_info=True)

    # -- stale decay -----------------------------------------------------------
    async def _decay_pass(self, report: Dict[str, Any], mode: str) -> None:
        stale = await stale_entities(self._world)
        if not stale:
            return
        # Decay is reversible by construction (bounded multiplicative drop,
        # floored at 0.1, never a deletion; prior value journaled below), so
        # the supervised rung may perform it.
        if mode not in ("live", "supervised"):
            logger.info("SHADOW-BELIEF decay: %d stale entit(ies) would "
                        "lose confidence", len(stale))
            return
        for e in stale[:100]:
            old = float(getattr(e, "confidence", 0.5) or 0.5)
            new = await decay_entity(self._world, e)
            report["decayed"] += 1
            if self._journal is not None:
                self._journal.record(
                    "beliefs",
                    f"decayed stale entity {getattr(e, 'name', '?')} "
                    f"confidence {old:.2f} -> {new:.2f}",
                    reasoning="unseen past TTL", decision="acted",
                    reversibility="recoverable",
                    ref=str(getattr(e, "id", "")))

    # -- reads -----------------------------------------------------------------
    def conflicts(self, status: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        return self.store.conflicts(status=status, limit=limit)

    def status(self) -> Dict[str, Any]:
        return {
            "mode": beliefs_mode(),
            "effective_mode": self._effective_mode(),
            "last_report": self.last_report,
            "open_conflicts": len(self.store.conflicts(status="open",
                                                       limit=1000)),
            "review_conflicts": len(self.store.conflicts(status="review",
                                                         limit=1000)),
            "recent_supersessions": self.store.supersessions(limit=10),
        }
