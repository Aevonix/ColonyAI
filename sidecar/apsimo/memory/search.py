"""Canonical collection and selection shared by recall and explicit search."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from typing import Any

from .recall import source_candidates, contact_fact_candidates, pack_memory_context

logger = logging.getLogger(__name__)


@dataclass
class CollectedSources:
    ledger: Any
    contact_id: str
    session_id: str
    watermark: int | None
    hits: list[dict[str, Any]] = field(default_factory=list)
    media: list[dict[str, Any]] = field(default_factory=list)
    semantic: str = 'unavailable'


@dataclass
class MemoryPacket:
    selected: list[dict[str, Any]]
    content: str
    source_refs: list[dict[str, str]]
    watermark: int | None
    retrieval: dict[str, str]
    annotation_checks: list[dict[str, Any]]

    def public(self) -> dict[str, Any]:
        return {'content': self.content, 'count': len(self.selected),
                'source_refs': self.source_refs, 'watermark': self.watermark,
                'retrieval': self.retrieval, 'annotation_checks': self.annotation_checks}


async def collect_sources(ledger, *, query: str, contact_id: str, session_id: str,
                          vector_store=None, embedding_pipeline=None) -> CollectedSources:
    """Read exact scoped sources; optional semantic failure keeps lexical evidence."""
    from apsimo.turns.source_vectors import SourceVectors, merge_source_hits
    collected = CollectedSources(ledger, contact_id, session_id,
                                ledger.erasure_watermark(contact_id) if ledger is not None else None)
    if ledger is None or not query.strip():
        return collected
    collected.hits = ledger.search_sources(query, contact_id=contact_id, session_id=session_id, limit=10)
    try:
        semantic_hits, collected.media = await SourceVectors(
            ledger, vector_store, embedding_pipeline).search(
                query, contact_id=contact_id, session_id=session_id, limit=15)
        collected.hits = merge_source_hits(collected.hits, semantic_hits)
        if vector_store is not None and embedding_pipeline is not None and getattr(vector_store, 'catalog', None) is not None:
            collected.semantic = 'ready'
    except Exception as exc:
        collected.semantic = 'failed'
        logger.debug('Source semantic recall unavailable (%s); retaining lexical evidence', type(exc).__name__)
    return collected


async def select_memory(collected: CollectedSources, *, query: str, selector,
                        extra_candidates=(), contact_facts=None, contact_facts_allowed=False,
                        timezone_name=None, current_work_available=False, limit=5) -> MemoryPacket:
    """Apply the existing projections, corrections, ranking and shared budget once.

    Optional existing noncanonical candidates are supplied by the caller. This
    module does not query a graph or infer an audience from a subject selector.
    """
    from apsimo.beliefs.source_time import interpret_time_query, filter_unstructured
    from apsimo.util import temporal
    ledger = collected.ledger
    scope = {'contact_id': collected.contact_id, 'session_id': collected.session_id}
    beliefs = list(extra_candidates)
    quotations = source_candidates(collected.hits)
    time_query = interpret_time_query(query, now=temporal.now_utc(), timezone_name=timezone_name)
    if ledger is not None:
        from apsimo.beliefs.source_projection import SourceClaimProjection
        from apsimo.memory.selection import current_work_query
        from apsimo.turns.media import SourceMedia
        beliefs, quotations = SourceClaimProjection(ledger).prepare_context(
            beliefs, collected.hits, **scope, time_query=time_query,
            classify_work_replies=current_work_available and current_work_query(query))
        media_hits = SourceMedia(ledger).search(query, **scope)
        media_by_id = {row['id']: row for row in media_hits + collected.media}
        quotations.extend(filter_unstructured(list(media_by_id.values()), time_query))
    else:
        beliefs = filter_unstructured(beliefs, time_query)
    facts_status = 'not_in_scope' if not contact_facts_allowed else 'unavailable'
    if contact_facts_allowed and contact_facts is not None:
        try:
            fact_result = contact_facts.list_facts(contact_id=collected.contact_id, limit=512)
            facts = fact_result if isinstance(fact_result, list) else fact_result.get('facts', [])
            quotations.extend(filter_unstructured(contact_fact_candidates(query, facts), time_query))
            facts_status = 'ready'
        except Exception as exc:
            logger.warning('Contact fact candidates unavailable (%s)', type(exc).__name__)
    try:
        max_chars = int(os.environ.get('COLONY_RECALL_CONTEXT_MAX_CHARS', '6000'))
    except (TypeError, ValueError):
        max_chars = 6000
    max_chars = max(0, min(max_chars, 24000))
    if ledger is not None:
        from apsimo.turns.source_annotations import expand, current_candidates
        beliefs = expand(ledger, beliefs, **scope)
        quotations = expand(ledger, quotations, covered=beliefs, **scope)
        # Lexical hits predate the optional embedding await. A removed source
        # or message has no current annotation membership; do not emit its old
        # text with empty references after expansion skips the missing source.
        quotations = [row for row in quotations if not row.get('source_turn_id')
                      or row['source_turn_id'] in {
                          ref['source_id'] for ref in row.get('_annotation_source_refs', [])}]
    selected, content = await selector.select_context(query, beliefs, quotations, limit=limit,
        current_work_available=current_work_available, max_chars=max_chars)
    if ledger is not None:
        retained = current_candidates(ledger, selected, **scope)
        if len(retained) != len(selected):
            selected, content = pack_memory_context(retained, limit=limit, max_chars=max_chars)
    source_ids = []
    for memory in selected:
        source_ids.extend(memory.get('source_turn_ids') or [])
        if memory.get('source_turn_id'):
            source_ids.append(memory['source_turn_id'])
        uri = str(memory.get('source_uri') or '')
        if uri.startswith('turn:'):
            source_ids.append(uri[5:])
    refs = ledger.source_references(source_ids, **scope) if ledger is not None else []
    refs_by_id = {ref['source_id']: ref for ref in refs}
    for memory in selected:
        # Keep the checked exact correction revision, never rebind old text to
        # a surviving version after an erasure.
        for ref in memory.get('_annotation_source_refs', []):
            refs_by_id[ref['source_id']] = ref
    checks = [{'source_refs': row['_annotation_source_refs'],
               'message_hashes': row['_annotation_message_hashes'],
               'annotation_ids': sorted(row['_annotation_ids'])}
              for row in selected if row.get('_annotation_source_refs')]
    return MemoryPacket(selected, content, list(refs_by_id.values()), collected.watermark,
                        {'semantic': collected.semantic, 'contact_facts': facts_status}, checks)
