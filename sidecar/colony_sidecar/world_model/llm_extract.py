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

from colony_sidecar.world_model.populator import _is_low_quality, _looks_like_fragment

logger = logging.getLogger(__name__)

_ENTITY_TYPES = {"person", "company", "project", "product", "location",
                 "event", "concept"}
_REL_TYPES = {"WM_WORKS_AT", "WM_KNOWS", "WM_PART_OF", "WM_RELATED_TO",
              "WM_LOCATED_IN", "WM_BUILDS"}
_MIN_CONF = 0.5

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
"confidence": float}]}"""


def llm_extract_mode() -> str:
    m = os.environ.get("COLONY_WORLD_LLM_EXTRACT", "off").strip().lower()
    return m if m in ("off", "shadow", "live") else "off"


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
                 directive_manager: Any = None, journal: Any = None) -> None:
        self._store = store
        self._graph = graph
        self._directives = directive_manager
        self._journal = journal
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
    async def _llm_batch(self, texts: List[str]) -> Optional[dict]:
        ep = _endpoint()
        if not ep["base"] or not ep["model"]:
            return None
        try:
            import aiohttp
        except ImportError:
            return None
        excerpt = "\n---\n".join(t[:600] for t in texts)[:8000]
        payload = {
            "model": ep["model"], "temperature": 0, "max_tokens": 900,
            "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                         {"role": "user", "content": excerpt}],
        }
        headers = {"Content-Type": "application/json"}
        if ep["key"]:
            headers["Authorization"] = f"Bearer {ep['key']}"
        try:
            timeout = aiohttp.ClientTimeout(total=float(
                os.environ.get("COLONY_WORLD_LLM_TIMEOUT", "60")))
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
            return True
        try:
            from colony_sidecar.directives import Action
            return self._directives.check(
                Action(kind="populate", text=name, target=name)).allowed
        except Exception:
            return True

    # -- main run -------------------------------------------------------------
    async def run(self, texts: Optional[List[str]] = None) -> Dict[str, Any]:
        mode = llm_extract_mode()
        report: Dict[str, Any] = {"mode": mode, "batches": 0, "created": [],
                                  "merged": [], "relationships": [],
                                  "skipped": 0}
        self.last_report = report
        if mode == "off" or self._store is None:
            return report
        self._seen_rels: set = set()
        if texts is None:
            texts = await self._recent_memory_texts()
        report["texts"] = len(texts or [])
        if not texts:
            logger.info("world-llm-extract[%s]: no recent memory texts to "
                        "process", mode)
            return report

        name_to_id: Dict[str, str] = {}
        for i in range(0, len(texts), 10):
            batch = texts[i:i + 10]
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
        logger.info(
            "world-llm-extract[%s]: %d batch(es), created=%d merged=%d "
            "rel=%d skipped=%d", mode, report["batches"],
            len(report["created"]), len(report["merged"]),
            len(report["relationships"]), report["skipped"])
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
            if mode == "live":
                try:
                    await self._store.add_entity_alias(matched, name)
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
        if mode != "live":
            return None
        try:
            from colony_sidecar.world_model.entities import ENTITY_CLASS_MAP, BaseEntity
            from colony_sidecar.world_model.sqlite.backend import _generate_id
            cls = ENTITY_CLASS_MAP.get(etype, BaseEntity)
            ent = cls(id=_generate_id("we"), name=name, entity_type=etype,
                      confidence=conf)
            await self._store.upsert_entity(ent)
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
        if mode != "live":
            return
        try:
            # Repeated mentions must corroborate, not duplicate: an existing
            # edge of the same type between the same pair is left alone.
            try:
                existing = await self._store.query_relationships(
                    source_id=src_id, target_id=tgt_id,
                    relationship_type=rel, min_confidence=0.0, limit=1)
            except Exception:
                existing = []
            if existing:
                return
            from colony_sidecar.world_model.relationships import WorldRelationship
            await self._store.upsert_relationship(WorldRelationship(
                id="", source_id=src_id, target_id=tgt_id,
                relationship_type=rel, confidence=min(0.7, conf)))
            self._journal_write(f"linked {src_id} -{rel}-> {tgt_id}", conf,
                                src_id)
        except Exception:
            logger.debug("llm-extract relationship upsert failed",
                         exc_info=True)

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
