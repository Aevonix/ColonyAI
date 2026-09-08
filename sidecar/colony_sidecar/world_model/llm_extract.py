"""LLM-assisted world-model extraction (batch, journaled).

Complements the rule-based WorldModelPopulator: on a daily batch cadence
(never per-turn), recent conversation memories are handed to a fast
classifier LLM which proposes structured entities + relationships as STRICT
JSON. Code validates everything (known types, name quality, confidence
floor, boundary check, resolver dedup) before any write, and every write is
recorded in the unified action journal so the owner can review exactly what
was added and why.

Endpoint: env-driven OpenAI-compatible chat endpoint
(COLONY_WORLD_LLM_BASE_URL / _MODEL / _API_KEY, falling back to the
COLONY_INTROSPECT_* fast-classifier endpoint convention).

Modes (COLONY_WORLD_LLM_EXTRACT): off (default) | shadow (log the would-be
writes) | live (write + journal).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from colony_sidecar.world_model.constants import CAUSAL_RELATIONSHIP_TYPES
from colony_sidecar.world_model.populator import _is_low_quality, _looks_like_fragment

logger = logging.getLogger(__name__)

_ENTITY_TYPES = {"person", "company", "project", "product", "location",
                 "event", "concept"}
_REL_TYPES = {"WM_WORKS_AT", "WM_KNOWS", "WM_PART_OF", "WM_RELATED_TO",
              "WM_LOCATED_IN", "WM_BUILDS"}
_MIN_CONF = 0.5

# Legacy causal extraction is a hypothesis with a conservative creation
# ceiling. Repeat batches are not independent predictive evidence.
_CAUSAL_CREATE_CEILING = 0.5

_SYSTEM_PROMPT = """\
You extract structured world knowledge from conversation excerpts for a
personal intelligence system. Extract only REAL named entities the owner's
world contains (people, companies, projects, products, locations, events,
concepts) and clearly stated relationships between them.

Rules:
- Only entities explicitly named in the text. Never invent, never guess.
- Skip generic words, greetings, dates, the assistant itself, and anything
  that is not a proper name.
- confidence reflects how clearly the text establishes the entity (0-1).
- Weight owner relevance: entities recurring in the excerpt or tied to the
  owner's work, projects, or relationships score higher; incidental one-off
  names score low (<=0.4).

Respond with ONLY a JSON object (no prose, no markdown fences):
{"entities": [{"name": str, "type": one of ["person","company","project",
"product","location","event","concept"], "confidence": float}],
"relationships": [{"source": str, "rel": one of ["WM_WORKS_AT","WM_KNOWS",
"WM_PART_OF","WM_RELATED_TO","WM_LOCATED_IN","WM_BUILDS"], "target": str,
"confidence": float}],
"observations": [{"entity": str, "property": "lowercase_property_key", "value": str, "temporal_status": "current",
"evidence": "exact verbatim sentence from excerpt containing both entity and value"}]}
Observations are reported claims only, never proof or direct sensor observations.
Only unqualified present-tense states belong in observations. Omit future, past,
dated, conditional or uncertain assertions; receiving a report today does not
make its described state current. Never clip a date/tense qualifier off a quote.
Do not infer personality, authority or trust. Omit vague, negated or uncertain claims.
Never turn a question, example, instruction or an assistant's paraphrase into a fact."""

_CAUSAL_PROMPT = """

Additionally extract clearly stated CAUSAL claims between those same
entities as "causal": [{"source": str, "rel": one of ["WM_CAUSES",
"WM_ENABLES","WM_BLOCKS","WM_INHIBITS"], "target": str, "evidence": str,
"confidence": float}].
Causal rules:
- Only when the text itself asserts the causal link. Never infer one.
- "evidence" MUST be a short verbatim quote copied character-for-character
  from the excerpt; a claim without its exact quote will be discarded."""


def llm_extract_mode() -> str:
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_WORLD_LLM_EXTRACT",
                   ("off", "shadow", "live"), "off")


def world_supervised_max_writes() -> int:
    """Per-run cap on supervised world-model writes (H1.5, default 25)."""
    try:
        v = int(os.environ.get("COLONY_WORLD_SUPERVISED_MAX_WRITES", "25"))
        return v if v > 0 else 25
    except (TypeError, ValueError):
        return 25


def causal_extract_mode() -> str:
    """Effective causal-extraction mode: min(COLONY_CAUSAL_EXTRACT,
    COLONY_WORLD_LLM_EXTRACT) over off < shadow < live — causal extraction
    can never be MORE live than the extractor that carries it."""
    from colony_sidecar.util.autonomy_preset import resolve
    order = ("off", "shadow", "live")
    causal = resolve("COLONY_CAUSAL_EXTRACT", order, "off")
    return order[min(order.index(causal), order.index(llm_extract_mode()))]


def _endpoint() -> Dict[str, str]:
    base = (os.environ.get("COLONY_WORLD_LLM_BASE_URL", "")
            or os.environ.get("COLONY_INTROSPECT_BASE_URL", "")).rstrip("/")
    model = (os.environ.get("COLONY_WORLD_LLM_MODEL", "")
             or os.environ.get("COLONY_INTROSPECT_MODEL", ""))
    key = (os.environ.get("COLONY_WORLD_LLM_API_KEY", "")
           or os.environ.get("COLONY_INTROSPECT_API_KEY", ""))
    return {"base": base, "model": model, "key": key}


def _parse_obj(content: str) -> Optional[dict]:
    text = (content or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class WorldLLMExtractor:
    def __init__(self, store: Any, *, graph: Any = None,
                 directive_manager: Any = None, journal: Any = None,
                 self_model: Any = None, source_ledger: Any = None, router_provider: Any = None) -> None:
        self._store = store
        self._graph = graph
        self._directives = directive_manager
        self._journal = journal
        self._self_model = self_model
        self._source_ledger = source_ledger
        self._router_provider = router_provider
        self._resolver = None
        if store is not None:
            try:
                from colony_sidecar.world_model.resolution.entity_resolver import (
                    EntityResolver,
                )
                self._resolver = EntityResolver(store)
            except Exception:
                pass
        self.last_report: Dict[str, Any] = {}

    # -- source text -------------------------------------------------------

    _RECENT_MEMORY_QUERY = (
        """MATCH (m:Memory)
                   WHERE m.created_at >= datetime() - duration({hours: $hours})
                     AND m.type IN ['episodic', 'semantic']
                     AND m.superseded_by IS NULL
                   RETURN m.content AS content
                   ORDER BY m.created_at DESC LIMIT $limit""")

    async def _recent_memory_texts(self, hours: float = 24.0,
                                   limit: int = 30) -> List[str]:
        if self._graph is None or not hasattr(self._graph, "run_query"):
            return []
        try:
            # Register this exact parameterized read query with the graph
            # client's Cypher allowlist (single-sourced here).
            if hasattr(type(self._graph), "register_allowed_cypher"):
                type(self._graph).register_allowed_cypher(
                    self._RECENT_MEMORY_QUERY)
            rows = await self._graph.run_query(
                self._RECENT_MEMORY_QUERY,
                {"hours": int(hours), "limit": int(limit)})
            return [str(r.get("content") or "") for r in rows or []
                    if r.get("content")]
        except Exception as exc:
            logger.info("world llm-extract memory query failed: %s", exc)
            return []

    # -- one LLM batch -------------------------------------------------------
    @staticmethod
    def _excerpt(texts: List[str]) -> str:
        """The exact excerpt sent to the LLM — also the anti-fabrication
        reference that causal evidence quotes are pinned against."""
        return "\n---\n".join(t[:600] for t in texts)[:8000]

    async def _llm_batch(self, texts: List[str]) -> Optional[dict]:
        if self._router_provider is not None:
            # Production uses the same discoverable extraction role as source
            # learning. Re-read the router each batch so rebinding needs no restart.
            import asyncio
            from colony_sidecar.beliefs.source_claims import final_text, extraction_timeout_seconds
            router = self._router_provider()
            if getattr(router, 'supports_function_routing', False) is not True:
                return None
            prompt = _SYSTEM_PROMPT + (_CAUSAL_PROMPT if causal_extract_mode() != 'off' else '')
            response = await asyncio.wait_for(router.complete(messages=[
                {'role': 'system', 'content': prompt}, {'role': 'user', 'content': self._excerpt(texts)}],
                context={'function_role': 'extraction', 'task': 'world_source_reports',
                         'max_output_tokens': 900, 'allow_fallback': True}),
                timeout=extraction_timeout_seconds(router))
            self.last_report['processor'] = {'function_role': 'extraction',
                'model_id': getattr(response, 'model_id', None),
                'model_revision': getattr(response, 'model_revision', None),
                'config_revision': getattr(response, 'config_revision', None)}
            return _parse_obj(final_text(response))
        ep = _endpoint()
        if not ep["base"] or not ep["model"]:
            return None
        try:
            import aiohttp
        except ImportError:
            return None
        excerpt = self._excerpt(texts)
        system_prompt = _SYSTEM_PROMPT
        if causal_extract_mode() != "off":
            system_prompt = _SYSTEM_PROMPT + _CAUSAL_PROMPT
        payload = {
            "model": ep["model"], "temperature": 0, "max_tokens": 900,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": excerpt}],
        }
        headers = {"Content-Type": "application/json"}
        if ep["key"]:
            headers["Authorization"] = f"Bearer {ep['key']}"
        try:
            # Keep the per-request timeout WELL under the autonomy tick
            # budget: when the tick's wait_for cancels mid-request, aiohttp's
            # session unwind gets interrupted and leaks ("Unclosed client
            # session" spam). A clean inner timeout aborts and closes.
            timeout = aiohttp.ClientTimeout(total=float(
                os.environ.get("COLONY_WORLD_LLM_TIMEOUT", "40")))
            async with aiohttp.ClientSession() as s:
                async with s.post(ep["base"] + "/chat/completions",
                                  json=payload, headers=headers,
                                  timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            return _parse_obj(data["choices"][0]["message"]["content"])
        except Exception as exc:
            logger.debug("world llm-extract call failed: %s", exc)
            return None

    def _boundary_ok(self, name: str) -> bool:
        if self._directives is None:
            # No directive manager configured: nothing to check against
            # (distinct from "checked and allowed" — see log line).
            logger.debug("world llm-extract boundary UNCHECKED for %r "
                         "(no directive manager)", name)
            return True
        try:
            from colony_sidecar.directives import Action
            return self._directives.check(
                Action(kind="populate", text=name, target=name)).allowed
        except Exception:
            # Fail closed, matching directives.guard.boundary_fail_closed():
            # an error while evaluating an owner boundary must refuse.
            logger.warning("world llm-extract boundary check errored for %r "
                           "— failing closed", name, exc_info=True)
            return False

    # -- supervised rung (H1.5) -----------------------------------------------

    def _effective_mode(self) -> str:
        """Env mode, graduated by the trust engine ONLY when the world_model
        domain is explicitly enrolled in COLONY_SUPERVISED_LIVE_DOMAINS —
        otherwise the env mode is returned untouched (regression lock: a
        deployment that never enrolls the domain sees zero behavior change,
        whatever the trust stage says)."""
        env = llm_extract_mode()
        try:
            from colony_sidecar.self_model.supervised import (
                effective_mode, supervised_domains,
            )
            if "world_model" not in supervised_domains():
                return env
            return effective_mode(
                "world_model", env,
                getattr(self._self_model, "trust", None))
        except Exception:
            return env

    def _may_write(self, mode: str, op: str, report: Dict[str, Any]) -> bool:
        """live => yes. supervised => only ops pinned reversible for the
        world_model domain, and only under the per-run write cap. Anything
        else => no."""
        if mode == "live":
            return True
        if mode != "supervised":
            return False
        from colony_sidecar.self_model.supervised import reversible
        if not reversible("world_model", op):
            return False
        if report.get("writes", 0) >= world_supervised_max_writes():
            report["supervised_capped"] = report.get("supervised_capped", 0) + 1
            return False
        return True

    def _record_outcome(self, mode: str, report: Dict[str, Any]) -> None:
        """Real trust outcomes for the world_model domain — but only when
        the domain is enrolled on the rung, and only when the run actually
        wrote something (a no-op run earns nothing; see H1.3)."""
        if self._self_model is None:
            return
        try:
            from colony_sidecar.self_model.supervised import supervised_domains
            if "world_model" not in supervised_domains():
                return
            if mode not in ("live", "supervised"):
                return
            if report.get("writes", 0) > 0:
                self._self_model.record("world_model", "success", shadow=False)
        except Exception:
            pass

    # -- main run -------------------------------------------------------------
    async def run(self, texts: Optional[List[str]] = None) -> Dict[str, Any]:
        mode = self._effective_mode()
        cmode = causal_extract_mode()
        report: Dict[str, Any] = {"mode": mode, "batches": 0, "created": [],
                                  "merged": [], "relationships": [],
                                  "skipped": 0, "causal_mode": cmode,
                                  "causal": [], "causal_corroborated": [],
                                  "causal_skipped": 0,
                                  "writes": 0, "supervised_capped": 0}
        self.last_report = report
        if mode == "off" or self._store is None:
            return report
        self._seen_rels: set = set()
        source_batches = None
        if texts is None and self._source_ledger is not None:
            from .source_reports import recent_batches
            source_batches = recent_batches(self._source_ledger)
            texts = [source['content'] for batch in source_batches for source in batch]
        elif texts is None:
            texts = await self._recent_memory_texts()
        report["texts"] = len(texts or [])
        if not texts:
            logger.info("world-llm-extract[%s]: no recent memory texts to "
                        "process", mode)
            return report

        batches = [([source['content'] for source in batch], batch) for batch in source_batches] if source_batches is not None else [
            (texts[i:i + 10], []) for i in range(0, len(texts), 10)]
        for batch, sources in batches:
            name_to_id: Dict[str, str] = {}
            data = await self._llm_batch(batch)
            if not data:
                continue
            report["batches"] += 1
            for e in (data.get("entities") or []):
                if not isinstance(e, dict):
                    continue
                name = str(e.get("name", "")).strip()[:120]
                etype = str(e.get("type", "")).strip().lower()
                try:
                    conf = float(e.get("confidence", 0))
                except (TypeError, ValueError):
                    conf = 0.0
                if (not name or etype not in _ENTITY_TYPES
                        or conf < _MIN_CONF
                        or _looks_like_fragment(name)
                        or _is_low_quality(name, etype)):
                    report["skipped"] += 1
                    continue
                if not self._boundary_ok(name):
                    report["skipped"] += 1
                    continue
                eid = await self._upsert(name, etype, conf, mode, report)
                if eid:
                    name_to_id[name.lower()] = eid
            from .source_reports import record_reports
            await record_reports(self, data.get('observations'), sources, name_to_id, mode, report)
            for r in (data.get("relationships") or []):
                if not isinstance(r, dict):
                    continue
                rel = str(r.get("rel", "")).strip().upper()
                src = str(r.get("source", "")).strip().lower()
                tgt = str(r.get("target", "")).strip().lower()
                if (rel not in _REL_TYPES or src not in name_to_id
                        or tgt not in name_to_id or src == tgt):
                    continue
                await self._upsert_rel(name_to_id[src], rel,
                                       name_to_id[tgt],
                                       float(r.get("confidence", 0.5) or 0.5),
                                       mode, report)
            if cmode != "off":
                excerpt_lower = self._excerpt(batch).lower()
                for c in (data.get("causal") or []):
                    if not isinstance(c, dict):
                        continue
                    rel = str(c.get("rel", "")).strip().upper()
                    src = str(c.get("source", "")).strip().lower()
                    tgt = str(c.get("target", "")).strip().lower()
                    evidence = str(c.get("evidence", "")).strip()
                    try:
                        conf = float(c.get("confidence", 0.5) or 0.5)
                    except (TypeError, ValueError):
                        conf = 0.5
                    if (rel not in CAUSAL_RELATIONSHIP_TYPES
                            or src not in name_to_id or tgt not in name_to_id
                            or src == tgt):
                        report["causal_skipped"] += 1
                        continue
                    # Anti-fabrication pin: the evidence quote must actually
                    # occur in the source excerpt (case-insensitive). A causal
                    # claim the model cannot quote is a fabrication and is
                    # discarded, whatever its confidence.
                    if not evidence or evidence.lower() not in excerpt_lower:
                        report["causal_skipped"] += 1
                        continue
                    await self._upsert_causal(
                        name_to_id[src], rel, name_to_id[tgt],
                        evidence, conf, cmode, report)
        logger.info(
            "world-llm-extract[%s]: %d batch(es), created=%d merged=%d "
            "rel=%d skipped=%d writes=%d", mode, report["batches"],
            len(report["created"]), len(report["merged"]),
            len(report["relationships"]), report["skipped"],
            report["writes"])
        self._record_outcome(mode, report)
        return report

    async def _upsert(self, name: str, etype: str, conf: float, mode: str,
                      report: Dict[str, Any]) -> Optional[str]:
        action, matched = "create", None
        if self._resolver is not None:
            try:
                from colony_sidecar.world_model.extraction.conversation_extractor import (
                    ExtractionCandidate,
                )
                cand = ExtractionCandidate(text=name, entity_type=etype,
                                           start_char=0, end_char=len(name),
                                           confidence=conf,
                                           context_window=name)
                res = await self._resolver.resolve(cand, etype)
                action = getattr(res.action, "value", str(res.action))
                matched = res.matched_entity_id
            except Exception:
                pass
        if action == "merge" and matched:
            report["merged"].append({"name": name, "into": matched})
            if self._may_write(mode, "alias_merge", report):
                try:
                    await self._store.add_entity_alias(matched, name)
                    report["writes"] = report.get("writes", 0) + 1
                except Exception:
                    pass
                self._journal_write(f"merged alias {name!r} into {matched}",
                                    conf, matched)
            return matched
        if action == "propose":
            report["skipped"] += 1
            return matched
        report["created"].append({"name": name, "type": etype,
                                  "confidence": round(conf, 2)})
        if not self._may_write(mode, "entity_upsert", report):
            return None
        try:
            from colony_sidecar.world_model.entities import ENTITY_CLASS_MAP, BaseEntity
            from colony_sidecar.world_model.sqlite.backend import _generate_id
            cls = ENTITY_CLASS_MAP.get(etype, BaseEntity)
            ent = cls(id=_generate_id("we"), name=name, entity_type=etype,
                      confidence=conf)
            await self._store.upsert_entity(ent)
            report["writes"] = report.get("writes", 0) + 1
            self._journal_write(f"created {etype} entity {name!r}", conf,
                                ent.id)
            return ent.id
        except Exception as exc:
            logger.debug("llm-extract upsert failed for %r: %s", name, exc)
            return None

    async def _upsert_rel(self, src_id: str, rel: str, tgt_id: str,
                          conf: float, mode: str,
                          report: Dict[str, Any]) -> None:
        key = (src_id, rel, tgt_id)
        if key in self._seen_rels:
            return
        self._seen_rels.add(key)
        report["relationships"].append(
            {"source": src_id, "rel": rel, "target": tgt_id})
        if mode not in ("live", "supervised"):
            return
        try:
            # Repeated batches do not create another edge or improve its
            # confidence. Qualified observations own new source evidence.
            try:
                existing = await self._store.query_relationships(
                    source_id=src_id, target_id=tgt_id,
                    relationship_type=rel, min_confidence=0.0, limit=1)
            except Exception:
                existing = []
            if existing:
                return
            # Creation remains an explicit live-mode operation.
            if not self._may_write(mode, "edge_create", report):
                return
            from colony_sidecar.world_model.relationships import WorldRelationship
            await self._store.upsert_relationship(WorldRelationship(
                id="", source_id=src_id, target_id=tgt_id,
                relationship_type=rel, confidence=min(0.7, conf)))
            report["writes"] = report.get("writes", 0) + 1
            self._journal_write(f"linked {src_id} -{rel}-> {tgt_id}", conf,
                                src_id)
        except Exception:
            logger.debug("llm-extract relationship upsert failed",
                         exc_info=True)

    async def _upsert_causal(self, src_id: str, rel: str, tgt_id: str,
                             evidence: str, conf: float, mode: str,
                             report: Dict[str, Any]) -> None:
        """Retain a reported causal hypothesis without repeat-confidence boosts.

        Repeat extraction does not refresh its support clock. Shadow writes no
        graph state; independent outcomes belong to the forecast lifecycle.
        """
        key = (src_id, rel, tgt_id)
        if key in self._seen_rels:
            return
        self._seen_rels.add(key)
        report["causal"].append({"source": src_id, "rel": rel,
                                 "target": tgt_id,
                                 "evidence": evidence[:200],
                                 "confidence": round(conf, 2)})
        if mode != "live":
            return
        try:
            try:
                existing = await self._store.query_relationships(
                    source_id=src_id, target_id=tgt_id,
                    relationship_type=rel, min_confidence=0.0, limit=1)
            except Exception:
                existing = []
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            if existing:
                report.setdefault("causal_repeated", []).append(existing[0].id)
                return
            from colony_sidecar.world_model.relationships import WorldRelationship
            create_conf = min(_CAUSAL_CREATE_CEILING, conf)
            created = await self._store.upsert_relationship(WorldRelationship(
                id="", source_id=src_id, target_id=tgt_id,
                relationship_type=rel, confidence=create_conf,
                properties={"evidence": evidence[:300],
                            "extraction": "llm_causal",
                            "last_support_at": now_iso}))
            self._journal_write(
                f"causal {src_id} -{rel}-> {tgt_id}", create_conf, src_id)
        except Exception:
            logger.debug("llm-extract causal upsert failed", exc_info=True)

    def _journal_write(self, description: str, conf: float, ref: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record(
                "world_model", description,
                reasoning="LLM extraction from recent conversation memories",
                confidence=conf, reversibility="reversible",
                decision="acted", ref=str(ref))
        except Exception:
            pass
