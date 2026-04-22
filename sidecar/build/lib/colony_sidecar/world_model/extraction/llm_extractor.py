"""LLM-backed entity extractor — the fallback path for ExtractionPipeline.

``ExtractionPipeline`` first tries each ``FormatExtractor``'s structured
``extract_entities`` pass. When that returns an empty list (common for
unstructured PDFs, plain HTML, free-form text) the pipeline falls back to
``extract_text`` + this LLM extractor.

The LLM is prompted to return a strict JSON array; anything else (prose,
partial JSON, network errors) degrades to an empty list so the pipeline
stays non-fatal.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from colony_sidecar.world_model.extraction.base import ExtractedEntity

logger = logging.getLogger(__name__)

LlmExtractFn = Callable[[str, Dict[str, Any]], Awaitable[List[ExtractedEntity]]]

# Plenty of room for a useful document but bounded so we don't blow tokens.
MAX_INPUT_CHARS = 12_000
MAX_RESPONSE_TOKENS = 1_024

_SYSTEM_PROMPT = (
    "You are an entity extraction engine. Read the document and return a "
    "JSON array of entities. Each entity must be an object with keys: "
    '"name" (string, required), "type" (one of: person, organization, '
    'place, concept, event, product), "attributes" (object, optional), '
    '"confidence" (number 0-1, optional). Return ONLY the JSON array — '
    "no prose, no code fences, no explanation. Return [] if no entities "
    "are present. Do not invent entities not grounded in the text."
)


def build_llm_extract_fn(
    llm_router: Any,
    *,
    max_input_chars: int = MAX_INPUT_CHARS,
    max_response_tokens: int = MAX_RESPONSE_TOKENS,
) -> LlmExtractFn:
    """Build an ``llm_extract_fn`` bound to the given ``LLMRouter``.

    Parameters
    ----------
    llm_router :
        An object with an ``async complete(messages, ...)`` method that
        returns an object with a ``.content`` attribute (the ``LLMRouter``
        from ``colony_sidecar.router.router``).
    max_input_chars :
        Truncate the text to this many characters before sending to the LLM.
    max_response_tokens :
        Cap on LLM output tokens.
    """

    async def extract(text: str, metadata: Dict[str, Any]) -> List[ExtractedEntity]:
        snippet = (text or "").strip()
        if not snippet:
            return []
        if len(snippet) > max_input_chars:
            snippet = snippet[:max_input_chars]
        user_prompt = _build_user_prompt(snippet, metadata)
        try:
            resp = await llm_router.complete(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                context={"task": "entity_extraction", "max_tokens": max_response_tokens},
            )
        except Exception as exc:
            logger.warning("LLM entity extraction call failed: %s", exc)
            return []

        content = getattr(resp, "content", "") or ""
        return _parse_entity_array(content)

    return extract


def _build_user_prompt(text: str, metadata: Dict[str, Any]) -> str:
    hints: List[str] = []
    filename = metadata.get("filename") or metadata.get("source")
    if filename:
        hints.append(f"Source: {filename}")
    if metadata.get("mime_type"):
        hints.append(f"MIME: {metadata['mime_type']}")
    prefix = ("\n".join(hints) + "\n\n") if hints else ""
    return f"{prefix}Document:\n---\n{text}\n---\n\nReturn the JSON array."


def _parse_entity_array(raw: str) -> List[ExtractedEntity]:
    """Best-effort parse of an LLM response into ``ExtractedEntity`` objects.

    Accepts either a bare JSON array or an object with an ``entities`` key
    (LLMs occasionally wrap the array). Tolerates code fences.
    """
    if not raw:
        return []
    candidate = _strip_code_fence(raw).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the first [...] block in the response.
        match = re.search(r"\[.*\]", candidate, re.DOTALL)
        if not match:
            logger.debug("LLM extractor: no JSON array in response")
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            logger.debug("LLM extractor: JSON parse failed (%s)", exc)
            return []

    if isinstance(parsed, dict):
        parsed = parsed.get("entities") or parsed.get("items") or []
    if not isinstance(parsed, list):
        return []

    entities: List[ExtractedEntity] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        entity_type = item.get("type") or item.get("entity_type")
        if not isinstance(name, str) or not isinstance(entity_type, str):
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            attributes = None
        confidence = item.get("confidence", 0.7)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.7
        entities.append(
            ExtractedEntity(
                name=name.strip(),
                entity_type=entity_type.strip(),
                attributes=attributes,
                confidence=max(0.0, min(1.0, confidence)),
            )
        )
    return entities


_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_code_fence(text: str) -> str:
    return _CODE_FENCE.sub("", text)
