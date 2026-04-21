"""Adaptive context compression for Colony.

Compresses enriched-context sections to fit within a token budget
while preserving the most relevant information. Three compression
modes with increasing aggression:

  - conservative: drop lowest-priority sections
  - balanced: drop sections + truncate body text
  - aggressive: drop + truncate + summarize with LLM

Compression is off by default. Enable via:
  COLONY_COMPRESSION_MODE=conservative|balanced|aggressive

Or per-request via the ``compression`` field on EnrichedContextRequest.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class CompressionMode(str, Enum):
    OFF = "off"
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


@dataclass
class CompressionConfig:
    mode: CompressionMode = CompressionMode.OFF
    max_tokens: int = 4000
    truncate_ratio: float = 0.6       # Fraction of body to keep when truncating
    min_section_tokens: int = 50       # Don't truncate below this
    relevance_boost: float = 1.5       # Boost multiplier for query-relevant sections
    preserve_ids: List[str] = field(default_factory=lambda: [
        "colony-identity",    # Always keep identity
    ])


def _default_config() -> CompressionConfig:
    mode_str = os.environ.get("COLONY_COMPRESSION_MODE", "off").lower()
    mode = CompressionMode(mode_str) if mode_str in (m.value for m in CompressionMode) else CompressionMode.OFF
    max_tokens = int(os.environ.get("COLONY_COMPRESSION_MAX_TOKENS", "4000"))
    return CompressionConfig(mode=mode, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token count: whitespace-split words + 15% overhead for subwords/punct."""
    if not text:
        return 0
    return max(1, int(len(text.split()) * 1.15))


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split into word set."""
    return set(re.findall(r"\w+", text.lower()))


def relevance_score(
    section_body: str,
    query: str,
    priority: int = 50,
    preserve: bool = False,
) -> float:
    """Score a section's relevance to the query.

    Combines keyword overlap with priority weighting.
    Preserved sections get a massive boost.

    Returns:
        Float score — higher means more worth keeping.
    """
    if preserve:
        return 1000.0

    query_tokens = _tokenize(query)
    if not query_tokens:
        return float(priority)

    body_tokens = _tokenize(section_body)
    if not body_tokens:
        return float(priority) * 0.5

    # Jaccard-like overlap
    overlap = len(query_tokens & body_tokens)
    recall = overlap / len(query_tokens)
    precision = overlap / max(len(body_tokens), 1)
    f1 = 2 * recall * precision / (recall + precision) if (recall + precision) > 0 else 0.0

    # Combine: 60% keyword relevance + 40% priority (normalized 0-1)
    priority_norm = priority / 100.0
    score = 0.6 * f1 + 0.4 * priority_norm

    return score


# ---------------------------------------------------------------------------
# Compression tiers
# ---------------------------------------------------------------------------

@dataclass
class SectionInfo:
    """Internal tracking for a section through compression."""
    id: str
    title: str
    body: str
    priority: int
    tokens: int
    score: float
    preserved: bool = False
    dropped: bool = False
    truncated: bool = False
    original_tokens: int = 0


def compress_sections(
    sections: List[Dict[str, Any]],
    query: str = "",
    config: Optional[CompressionConfig] = None,
    override_mode: Optional[CompressionMode] = None,
) -> Dict[str, Any]:
    """Compress enriched-context sections to fit a token budget.

    Args:
        sections: List of ContextSection dicts (id, title, body, priority).
        query: The user message / query for relevance scoring.
        config: Compression config (uses env defaults if None).
        override_mode: Per-request mode override from the API.

    Returns:
        Dict with:
          - sections: compressed list of section dicts
          - stats: compression statistics
          - metadata: debug info
    """
    cfg = config or _default_config()
    mode = override_mode or cfg.mode

    if mode == CompressionMode.OFF or not sections:
        return {
            "sections": sections,
            "stats": _stats(sections, sections, mode.value),
            "metadata": {"mode": mode.value, "applied": False},
        }

    # Build section info with scores
    infos: List[SectionInfo] = []
    for s in sections:
        body = s.get("body", "")
        title = s.get("title", "") or ""
        priority = s.get("priority") or 50
        sid = s.get("id", "")
        preserve = sid in cfg.preserve_ids
        tokens = estimate_tokens(f"{title} {body}")

        infos.append(SectionInfo(
            id=sid,
            title=title,
            body=body,
            priority=priority,
            tokens=tokens,
            original_tokens=tokens,
            score=relevance_score(body, query, priority, preserve),
            preserved=preserve,
        ))

    # Sort by score descending (highest relevance first)
    infos.sort(key=lambda i: i.score, reverse=True)

    budget = cfg.max_tokens

    # --- Tier 1: Drop low-relevance sections ---
    if mode in (CompressionMode.CONSERVATIVE, CompressionMode.BALANCED, CompressionMode.AGGRESSIVE):
        infos = _tier1_drop(infos, budget)

    # --- Tier 2: Truncate body text of surviving sections ---
    if mode in (CompressionMode.BALANCED, CompressionMode.AGGRESSIVE):
        infos = _tier2_truncate(infos, budget, cfg)

    # --- Tier 3: tighter truncation (aggressive only, sync path) ---
    # LLM summarization is the preferred aggressive tactic — see the async
    # ``compress_sections_with_llm`` wrapper below. When no LLM is
    # available we fall back to this deterministic tight-truncation tier
    # so aggressive mode always produces *some* size reduction.
    if mode == CompressionMode.AGGRESSIVE:
        infos = _tier3_tight_truncate(infos, budget, cfg)

    # Build result
    result_sections = []
    for info in infos:
        if info.dropped:
            continue
        result_sections.append({
            "id": info.id,
            "title": info.title,
            "body": info.body,
            "priority": info.priority,
        })

    return {
        "sections": result_sections,
        "stats": _stats(sections, result_sections, mode.value),
        "metadata": {
            "mode": mode.value,
            "applied": True,
            "dropped": [i.id for i in infos if i.dropped],
            "truncated": [i.id for i in infos if i.truncated],
        },
    }


def _tier1_drop(infos: List[SectionInfo], budget: int) -> List[SectionInfo]:
    """Drop lowest-scoring sections until we fit the budget.

    Only drops sections if there are enough others to potentially
    fit the budget. If we'd have to drop everything, leave them
    all for tier2 truncation instead.
    """
    kept = [i for i in infos if not i.dropped]
    total = sum(i.tokens for i in kept)
    if total <= budget:
        return infos

    # Don't drop below 1 section — let truncation handle it
    droppable = [i for i in kept if not i.preserved]
    if len(droppable) >= len(kept):
        # Everything is droppable; only drop if we'd still have >= 1 left
        pass

    for info in reversed(infos):
        if info.preserved or info.dropped:
            continue
        # Don't drop the last non-preserved section
        remaining = sum(1 for i in infos if not i.dropped and not i.preserved and i is not info)
        if remaining < 1 and not any(i.preserved for i in infos if not i.dropped):
            break
        info.dropped = True
        total -= info.tokens
        if total <= budget:
            break

    return infos


def _tier2_truncate(infos: List[SectionInfo], budget: int, cfg: CompressionConfig) -> List[SectionInfo]:
    """Truncate body text of sections still over budget."""
    kept = [i for i in infos if not i.dropped]
    total = sum(i.tokens for i in kept)
    if total <= budget:
        return infos

    # Calculate how much we need to cut
    overage = total - budget
    # Distribute truncation proportionally, skipping preserved
    truncatable = [i for i in kept if not i.preserved and i.tokens > cfg.min_section_tokens]
    if not truncatable:
        return infos

    truncatable_tokens = sum(i.tokens for i in truncatable)
    for info in truncatable:
        # Proportional share of overage
        share = overage * (info.tokens / truncatable_tokens)
        target_tokens = max(cfg.min_section_tokens, int(info.tokens - share))
        # Truncate body text to approximate target
        target_chars = int(len(info.body) * (target_tokens / max(info.tokens, 1)))
        info.body = _truncate_text(info.body, target_chars)
        info.tokens = estimate_tokens(f"{info.title} {info.body}")
        info.truncated = True

    return infos


def _tier3_tight_truncate(infos: List[SectionInfo], budget: int, cfg: CompressionConfig) -> List[SectionInfo]:
    """Aggressive mode: tighter truncation if still over budget."""
    kept = [i for i in infos if not i.dropped]
    total = sum(i.tokens for i in kept)
    if total <= budget:
        return infos

    # Cut all truncatable sections to min_section_tokens
    for info in kept:
        if info.preserved or info.tokens <= cfg.min_section_tokens:
            continue
        target_chars = int(len(info.body) * (cfg.min_section_tokens / max(info.tokens, 1)))
        info.body = _truncate_text(info.body, target_chars)
        info.tokens = estimate_tokens(f"{info.title} {info.body}")
        info.truncated = True

    return infos


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, breaking at sentence boundary if possible."""
    if len(text) <= max_chars:
        return text

    # Try to break at the last sentence ending before max_chars
    truncated = text[:max_chars]
    # Check for sentence endings in reverse order of preference
    for sep in (". ", "! ", "? ", "\n"):
        last = truncated.rfind(sep)
        if last > max_chars * 0.5:
            return truncated[: last + 1].strip()  # Keep the period, drop trailing space

    # Fall back to word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        return truncated[:last_space].strip() + " ..."

    return truncated.strip() + " ..."


def _stats(
    original: List[Dict[str, Any]],
    result: List[Dict[str, Any]],
    mode: str,
) -> Dict[str, Any]:
    """Compute before/after compression statistics."""
    orig_tokens = sum(estimate_tokens(s.get("body", "")) for s in original)
    result_tokens = sum(estimate_tokens(s.get("body", "")) for s in result)
    ratio = result_tokens / orig_tokens if orig_tokens > 0 else 1.0

    return {
        "mode": mode,
        "original_sections": len(original),
        "result_sections": len(result),
        "original_tokens": orig_tokens,
        "result_tokens": result_tokens,
        "compression_ratio": round(ratio, 2),
        "saved_tokens": orig_tokens - result_tokens,
    }


# ---------------------------------------------------------------------------
# Tier 3: LLM summarization (aggressive mode)
# ---------------------------------------------------------------------------

_SUMMARIZE_SYSTEM_PROMPT = (
    "You compress long context sections for an AI assistant that has a limited "
    "token budget. Rewrite the user's section to preserve every fact, name, "
    "number, and decision — but drop filler, redundancy, and stylistic prose. "
    "Return ONLY the compressed section body; no preamble, no headings, no "
    "commentary about what you changed. Stay under the requested token budget."
)


async def _llm_summarize_body(
    llm_router: Any,
    *,
    title: str,
    body: str,
    target_tokens: int,
) -> Optional[str]:
    """Ask the LLM to rewrite ``body`` under ``target_tokens``.

    Returns the summary on success or ``None`` on any failure — callers fall
    back to the tight-truncated body.
    """
    if not body or target_tokens <= 0:
        return None
    user_prompt = (
        f"Section title: {title or '(untitled)'}\n"
        f"Target: ~{target_tokens} tokens\n\n"
        f"Section body:\n---\n{body}\n---\n\n"
        f"Compressed body (plain text, no headings):"
    )
    try:
        resp = await llm_router.complete(
            messages=[
                {"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            context={"task": "context_compression", "max_tokens": max(64, target_tokens * 2)},
        )
    except Exception as exc:
        logger.debug("LLM summarize call failed for '%s': %s", title, exc)
        return None

    summary = (getattr(resp, "content", "") or "").strip()
    if not summary:
        return None
    # Trim accidental code fences / wrapping.
    summary = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", summary).strip()
    return summary or None


async def compress_sections_with_llm(
    sections: List[Dict[str, Any]],
    *,
    llm_router: Any,
    query: str = "",
    config: Optional[CompressionConfig] = None,
    override_mode: Optional[CompressionMode] = None,
) -> Dict[str, Any]:
    """Async variant of ``compress_sections`` that applies LLM summarization.

    Runs the full sync pipeline first (tier 1 drop + tier 2 truncate +
    tier 3 tight truncate) and then — if mode is AGGRESSIVE and the LLM
    is reachable — replaces truncated section bodies with LLM-generated
    summaries. Falls back to the sync result on any LLM error so the
    caller always gets a useful response.
    """
    result = compress_sections(
        sections,
        query=query,
        config=config,
        override_mode=override_mode,
    )
    mode_str = result["metadata"]["mode"]
    if mode_str != CompressionMode.AGGRESSIVE.value or llm_router is None:
        return result

    cfg = config or _default_config()
    truncated_ids = set(result["metadata"].get("truncated") or [])
    if not truncated_ids:
        return result

    summarized_ids: List[str] = []
    for section in result["sections"]:
        sid = section.get("id", "")
        if sid not in truncated_ids:
            continue
        body = section.get("body", "") or ""
        target = max(cfg.min_section_tokens, int(estimate_tokens(body)))
        summary = await _llm_summarize_body(
            llm_router,
            title=section.get("title", "") or "",
            body=body,
            target_tokens=target,
        )
        if summary is None:
            continue
        section["body"] = summary
        summarized_ids.append(sid)

    if summarized_ids:
        result["metadata"]["summarized"] = summarized_ids
        # Recompute stats to reflect the LLM pass.
        originals = list(sections)
        result["stats"] = _stats(originals, result["sections"], mode_str)

    return result
