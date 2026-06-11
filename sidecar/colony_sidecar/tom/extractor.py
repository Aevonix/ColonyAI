"""LLM-backed extraction for Theory of Mind.

Extracts affect signals and shared facts from conversation turns.
Uses the LLM router (same as world model entity extraction) — Colony
does NOT build its own LLM client.

Throttled per contact: max 1 extraction per COLONY_TOM_EXTRACTION_THROTTLE_MINUTES
(default 5 minutes) per contact.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 4_000
MAX_RESPONSE_TOKENS = int(os.environ.get("COLONY_TOM_MAX_TOKENS", "2048"))  # reasoning models (mimo) need room; 512 returns empty content
THROTTLE_MINUTES = int(os.environ.get("COLONY_TOM_EXTRACTION_THROTTLE_MINUTES", "5"))

# ---------------------------------------------------------------------------
# Affect extraction prompt
# ---------------------------------------------------------------------------

_AFFECT_SYSTEM_PROMPT = (
    "You are an emotion analysis engine. Read the conversation and return a "
    "JSON object with the speaker's emotional state. Keys: "
    '"valence" (float -1.0 to 1.0, negative=unhappy, positive=happy), '
    '"arousal" (float 0.0 to 1.0, 0=calm, 1=intense), '
    '"trigger" (string, brief excerpt causing the reading), '
    '"confidence" (float 0.0 to 1.0). '
    "Return ONLY the JSON object. If the conversation is neutral, "
    'return {"valence": 0.0, "arousal": 0.3, "trigger": null, "confidence": 0.5}. '
    "Do not add prose, code fences, or explanation."
)

# ---------------------------------------------------------------------------
# Fact extraction prompt
# ---------------------------------------------------------------------------

_FACT_SYSTEM_PROMPT = (
    "You are a knowledge extraction engine. Read the conversation and identify "
    "what the contact now KNOWS or BELIEVES as a result of this conversation. "
    "Return a JSON array of objects. Each object has keys: "
    '"fact" (string, the knowledge item), '
    '"source" (one of: told_by_contact, told_to_contact, shared_context, inferred), '
    '"confidence" (float 0.0 to 1.0). '
    "Return ONLY the JSON array. Return [] if no new knowledge. "
    "Do not add prose, code fences, or explanation."
)


_ENGAGEMENT_SYSTEM_PROMPT = (
    "You build a profile of how to communicate effectively with a specific person. "
    "Read the conversation and infer, ONLY where there is clear evidence, the "
    "CONTACT's (not the assistant's) psychology and communication style. "
    "Return ONE JSON object with these optional keys: "
    '"ocean": object with any of openness, conscientiousness, extraversion, '
    "agreeableness, neuroticism, each a float -1.0..1.0 (high=+1.0), omit a trait "
    "if there is no evidence; "
    '"style": object with any of formality (0=casual,1=formal), directness '
    "(0=indirect,1=blunt), warmth (0=cool,1=warm), verbosity (0=terse,1=expansive), "
    "emoji_ok (0=never,1=loves), humor (0=serious,1=playful), omit if no evidence; "
    '"motivators": array of short strings (what drives or engages them); '
    '"topics": array of short strings (subjects they engage on); '
    '"avoid": array of short strings (what to avoid when communicating with them); '
    '"confidence": float 0.0..1.0. '
    "Base every value strictly on evidence in THIS conversation; omit anything you "
    "would only be guessing. Return ONLY the JSON object, no prose or code fences."
)


class TomExtractor:
    """LLM-backed ToM extraction from conversation turns."""

    def __init__(self, llm_router: Any) -> None:
        self._router = llm_router
        self._last_extraction: Dict[str, str] = {}  # contact_id → ISO timestamp
        self._last_engagement: Dict[str, str] = {}  # separate throttle for engagement

    async def extract_affect(
        self,
        conversation_text: str,
        contact_id: str,
        *,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Extract affect from a conversation snippet.

        Returns dict with valence, arousal, trigger, confidence or None.
        Throttled per contact.
        """
        if not self._can_extract(contact_id):
            return None

        snippet = (conversation_text or "").strip()
        if not snippet:
            return None
        if len(snippet) > MAX_INPUT_CHARS:
            snippet = snippet[:MAX_INPUT_CHARS]

        user_prompt = f"Conversation:\n---\n{snippet}\n---\n\nAnalyze the speaker's emotional state."

        try:
            resp = await self._router.complete(
                messages=[
                    {"role": "system", "content": _AFFECT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                context={"task": "tom_affect_extraction", "max_tokens": MAX_RESPONSE_TOKENS},
            )
        except Exception as exc:
            logger.warning("ToM affect extraction LLM call failed: %s", exc)
            return None

        content = getattr(resp, "content", "") or ""
        result = _parse_affect_json(content)
        if result is not None:
            self._mark_extracted(contact_id)
            result["source"] = "inferred"
            result["contact_id"] = contact_id
            if session_id:
                result["session_id"] = session_id
        return result

    async def extract_facts(
        self,
        conversation_text: str,
        contact_id: str,
        *,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Extract shared facts from a conversation snippet.

        Returns list of dicts with fact, source, confidence.
        Throttled per contact.
        """
        if not self._can_extract(contact_id):
            return []

        snippet = (conversation_text or "").strip()
        if not snippet:
            return []
        if len(snippet) > MAX_INPUT_CHARS:
            snippet = snippet[:MAX_INPUT_CHARS]

        user_prompt = f"Conversation:\n---\n{snippet}\n---\n\nWhat does the contact now know?"

        try:
            resp = await self._router.complete(
                messages=[
                    {"role": "system", "content": _FACT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                context={"task": "tom_fact_extraction", "max_tokens": MAX_RESPONSE_TOKENS},
            )
        except Exception as exc:
            logger.warning("ToM fact extraction LLM call failed: %s", exc)
            return []

        content = getattr(resp, "content", "") or ""
        results = _parse_fact_array(content)
        self._mark_extracted(contact_id)
        for r in results:
            r["contact_id"] = contact_id
        return results

    async def extract_engagement(
        self,
        conversation_text: str,
        contact_id: str,
        *,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Extract OCEAN + communication-style observations about the contact.

        Returns {ocean, style, motivators, topics, avoid} or None. Throttled per
        contact independently of affect/fact extraction.
        """
        last = self._last_engagement.get(contact_id)
        if last:
            try:
                from datetime import datetime as _dt
                age_min = (datetime.now(timezone.utc) - _dt.fromisoformat(last)).total_seconds() / 60.0
                if age_min < THROTTLE_MINUTES:
                    return None
            except Exception:
                pass

        snippet = (conversation_text or "").strip()
        if not snippet:
            return None
        if len(snippet) > MAX_INPUT_CHARS:
            snippet = snippet[:MAX_INPUT_CHARS]

        user_prompt = (
            f"Conversation:\n---\n{snippet}\n---\n\n"
            "Profile the CONTACT's psychology and communication style."
        )
        try:
            resp = await self._router.complete(
                messages=[
                    {"role": "system", "content": _ENGAGEMENT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                context={"task": "tom_engagement_extraction", "max_tokens": MAX_RESPONSE_TOKENS},
            )
        except Exception as exc:
            logger.warning("ToM engagement extraction LLM call failed: %s", exc)
            return None

        content = getattr(resp, "content", "") or ""
        result = _parse_engagement_json(content)
        if result is not None:
            self._last_engagement[contact_id] = datetime.now(timezone.utc).isoformat()
            result["contact_id"] = contact_id
            if session_id:
                result["session_id"] = session_id
        return result

    def _can_extract(self, contact_id: str) -> bool:
        """Check throttle: min THROTTLE_MINUTES between extractions per contact."""
        last = self._last_extraction.get(contact_id)
        if last is None:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            return elapsed >= THROTTLE_MINUTES
        except (ValueError, TypeError):
            return True

    def _mark_extracted(self, contact_id: str) -> None:
        self._last_extraction[contact_id] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_code_fence(text: str) -> str:
    return _CODE_FENCE.sub("", text)


def _parse_engagement_json(raw: str) -> Optional[Dict[str, Any]]:
    candidate = _strip_code_fence((raw or "").strip())
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("ocean", "style"):
        val = parsed.get(key)
        if isinstance(val, dict):
            clean = {}
            for k, v in val.items():
                try:
                    clean[str(k).strip().lower()] = float(v)
                except (TypeError, ValueError):
                    continue
            if clean:
                out[key] = clean
    for key in ("motivators", "topics", "avoid"):
        val = parsed.get(key)
        if isinstance(val, list):
            items = [str(x).strip() for x in val if str(x).strip()]
            if items:
                out[key] = items[:8]
    if not out:
        return None
    return out


def _parse_affect_json(raw: str) -> Optional[Dict[str, Any]]:
    """Parse LLM response into affect dict."""
    if not raw:
        return None
    candidate = _strip_code_fence(raw).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Try to find JSON object in response
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, dict):
        return None

    valence = parsed.get("valence", 0.0)
    arousal = parsed.get("arousal", 0.3)
    confidence = parsed.get("confidence", 0.5)

    try:
        valence = max(-1.0, min(1.0, float(valence)))
        arousal = max(0.0, min(1.0, float(arousal)))
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        return None

    # Skip neutral readings — not worth storing
    if abs(valence) < 0.1 and abs(arousal - 0.3) < 0.1:
        return None

    return {
        "valence": valence,
        "arousal": arousal,
        "trigger": parsed.get("trigger"),
        "confidence": confidence,
    }


def _parse_fact_array(raw: str) -> List[Dict[str, Any]]:
    """Parse LLM response into list of fact dicts."""
    if not raw:
        return []
    candidate = _strip_code_fence(raw).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", candidate, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    if isinstance(parsed, dict):
        parsed = parsed.get("facts") or parsed.get("items") or []
    if not isinstance(parsed, list):
        return []

    facts: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fact_text = item.get("fact")
        if not isinstance(fact_text, str) or not fact_text.strip():
            continue
        source = item.get("source", "inferred")
        if source not in ("told_by_contact", "told_to_contact", "shared_context", "inferred"):
            source = "inferred"
        confidence = item.get("confidence", 0.7)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.7
        facts.append({
            "fact": fact_text.strip(),
            "source": source,
            "confidence": confidence,
        })
    return facts
