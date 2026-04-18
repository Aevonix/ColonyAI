"""World Model background extraction and resolution jobs."""

import base64
from typing import Any, Dict, Optional, TYPE_CHECKING

from colony_sidecar.task_queue.worker import JobHandler
from colony_sidecar.task_queue.models import Job, JobResult, JobStatus

from ..extraction.document_extractor import DocumentExtractor, DocumentType
from ..extraction.conversation_extractor import ExtractionCandidate
from ..resolution.entity_resolver import EntityResolver, ResolutionAction
from ..entities import BaseEntity, ENTITY_CLASS_MAP
from ..confidence import CONFIDENCE_BY_SOURCE
from ..sqlite.backend import _generate_id
from ..constants import ENTITY_ID_PREFIX

if TYPE_CHECKING:
    from ..store import WorldModelStore


class DocumentExtractionJob(JobHandler):
    """Background job: extract entities from a document.

    Triggered by: document attachment processing, web page fetch,
    email body analysis.

    Payload keys:
      - source_reference: str (URL, file path, or email ID)
      - document_type: str (DocumentType value)
      - content_b64: str (base64-encoded document content)
      - priority: str ("high" | "normal" | "low")
    """

    job_type = "world_model.document_extraction"

    def __init__(self, store: "WorldModelStore") -> None:
        self._store = store
        self._extractor = DocumentExtractor()
        self._resolver = EntityResolver(store)

    async def execute(self, job: Job) -> Dict[str, Any]:
        """Execute document entity extraction."""
        payload = job.payload
        source_reference = payload.get("source_reference", "")
        doc_type_str = payload.get("document_type", "plain_text")
        content_b64 = payload.get("content_b64", "")

        try:
            doc_type = DocumentType(doc_type_str)
        except ValueError:
            doc_type = DocumentType.PLAIN_TEXT

        content = base64.b64decode(content_b64) if content_b64 else b""

        extraction_result = await self._extractor.extract_from_document(
            content=content,
            document_type=doc_type,
            source_reference=source_reference,
        )

        extracted_count = len(extraction_result.entities)
        linked_count = 0

        for candidate in extraction_result.entities:
            if candidate.confidence < self._store._config.min_confidence_for_storage:
                continue

            resolution = await self._resolver.resolve(
                candidate, candidate.entity_type
            )

            if resolution.action == ResolutionAction.MERGE and resolution.matched_entity_id:
                linked_count += 1
            elif resolution.action == ResolutionAction.CREATE:
                cls = ENTITY_CLASS_MAP.get(candidate.entity_type, BaseEntity)
                new_entity = cls(
                    id=_generate_id(ENTITY_ID_PREFIX),
                    name=candidate.text,
                    entity_type=candidate.entity_type,
                    confidence=candidate.confidence,
                )
                await self._store.upsert_entity(new_entity)

        return {
            "source_reference": source_reference,
            "extracted_count": extracted_count,
            "linked_count": linked_count,
        }


class EntityResolutionJob(JobHandler):
    """Background job: run entity resolution on a batch of candidates.

    Triggered by: conversation extraction completing, large import.

    Payload keys:
      - candidates: list of {text, entity_type, confidence} dicts
      - source_id: str
    """

    job_type = "world_model.entity_resolution"

    def __init__(self, store: "WorldModelStore") -> None:
        self._store = store
        self._resolver = EntityResolver(store)

    async def execute(self, job: Job) -> Dict[str, Any]:
        """Execute batch entity resolution."""
        payload = job.payload
        candidates_raw = payload.get("candidates", [])

        merged = 0
        created = 0
        proposed = 0

        for raw in candidates_raw:
            candidate = ExtractionCandidate(
                text=raw.get("text", ""),
                entity_type=raw.get("entity_type", "concept"),
                start_char=raw.get("start_char", 0),
                end_char=raw.get("end_char", 0),
                confidence=raw.get("confidence", 0.30),
                context_window=raw.get("context_window", ""),
            )
            if not candidate.text:
                continue

            resolution = await self._resolver.resolve(
                candidate, candidate.entity_type
            )

            if resolution.action == ResolutionAction.MERGE:
                merged += 1
            elif resolution.action == ResolutionAction.PROPOSE_MERGE:
                from ..resolution.merge_workflow import MergeWorkflow
                workflow = MergeWorkflow(self._store)
                # Create a placeholder entity for the candidate
                cls = ENTITY_CLASS_MAP.get(candidate.entity_type, BaseEntity)
                new_entity = cls(
                    id=_generate_id(ENTITY_ID_PREFIX),
                    name=candidate.text,
                    entity_type=candidate.entity_type,
                    confidence=candidate.confidence,
                )
                await self._store.upsert_entity(new_entity)
                await workflow.propose_merge(
                    candidate_id=new_entity.id,
                    existing_id=resolution.matched_entity_id,
                    confidence=resolution.match_confidence,
                    reason=resolution.match_reason,
                )
                proposed += 1
            elif resolution.action == ResolutionAction.CREATE:
                cls = ENTITY_CLASS_MAP.get(candidate.entity_type, BaseEntity)
                new_entity = cls(
                    id=_generate_id(ENTITY_ID_PREFIX),
                    name=candidate.text,
                    entity_type=candidate.entity_type,
                    confidence=candidate.confidence,
                )
                await self._store.upsert_entity(new_entity)
                created += 1

        return {
            "merged": merged,
            "created": created,
            "proposed": proposed,
        }
