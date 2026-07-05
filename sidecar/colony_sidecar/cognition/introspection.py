"""Inline per-turn introspection.

After a turn is synced, judge it with a local LLM and record any owed follow-up
directly — durable commitments and, above all, IMMEDIATE OWED DELIVERABLES ("text me
the result") — without depending on an external cognition subagent. This is the
sidecar-side counterpart to the event-based ``cognition.requested`` path: that path
emits an event for a host plugin to consume, which not every deployment wires up; this
path runs the judgment in-process against any OpenAI-compatible chat endpoint.

Disabled by default. Activated with ``COLONY_INTROSPECT_ENABLED=true`` and a configured
endpoint; deployment-agnostic (point ``COLONY_INTROSPECT_BASE_URL`` at a local model):

  COLONY_INTROSPECT_ENABLED     "true"|"false"      (default false)
  COLONY_INTROSPECT_BASE_URL    OpenAI-compatible base, e.g. http://127.0.0.1:8000/v1
  COLONY_INTROSPECT_MODEL       model id the endpoint serves (required to run)
  COLONY_INTROSPECT_API_KEY     bearer token, blank for a local model
  COLONY_INTROSPECT_TIMEOUT     seconds (default 30)
  COLONY_INTROSPECT_MAX_TOKENS  response cap (default 400)

Because an inline call cannot use tools, it uses a focused JSON-only prompt and creates
the commitments itself.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Purpose-built, JSON-only prompt (the tool-calling COGNITION_SYSTEM_PROMPT is too hedged and
# mentions tools, which makes a no-think local model return nothing). Self-contained.
_SYSTEM_PROMPT = (
    "You audit ONE finished assistant turn and extract any follow-up worth recording, as STRICT JSON.\n"
    "You get what the person SAID and what the assistant REPLIED. Decide only from the literal words.\n\n"
    "Record an item only when the turn clearly contains one of:\n"
    "1. A DURABLE COMMITMENT — an explicit promise, obligation, or reminder to do something later "
    "(\"remind me to X\", \"I'll get back to you on X\", \"follow up on X by Friday\", \"we need to fix X\").\n"
    "2. An IMMEDIATE OWED DELIVERABLE — the person asked to be SENT something through a channel the reply "
    "did NOT satisfy (email it, text a DIFFERENT number, send it to someone else, send it later), AND the "
    "actual content to send is present in the exchange. IMPORTANT: in a chat the assistant's reply already "
    "IS a message to the person, so a plain \"text me\"/\"message me\" is ALREADY satisfied — do NOT record "
    "that; only record a deliverable for a genuinely different channel, recipient, or time.\n\n"
    "Do NOT record small talk, questions, hypotheticals, vague intentions, or anything the reply already "
    "fully handled. Fewer items beats wrong items.\n\n"
    "Output ONLY a JSON array, nothing else (no prose, no markdown, no code fence). Empty array [] when "
    "nothing qualifies. Each element:\n"
    '{"description": string, "due_at": ISO-8601-UTC string or null, "priority": integer 0-100, '
    '"source_type": "cognition" | "introspection", "metadata": null or '
    '{"kind":"deliverable","content":"<exact text to send, ready as-is>","channel_hint":"sms"|"dm"|"email"}}\n'
    "Use \"introspection\" + the deliverable metadata (due_at about two minutes from now) for case 2; "
    "\"cognition\" + metadata null for case 1.\n\n"
    "Examples:\n"
    "They said: Remind me to call the dentist Friday at 9am. | Assistant replied: Got it.\n"
    '[{"description":"Remind them to call the dentist Friday 9am","due_at":"2026-06-26T13:00:00+00:00",'
    '"priority":70,"source_type":"cognition","metadata":null}]\n'
    "They said: Email me the Q3 revenue number. | Assistant replied: Q3 revenue was 4.2 million.\n"
    '[{"description":"Email them the Q3 revenue","due_at":"2026-06-21T21:40:00+00:00","priority":80,'
    '"source_type":"introspection","metadata":{"kind":"deliverable","content":"Q3 revenue was 4.2 million.",'
    '"channel_hint":"email"}}]\n'
    "They said: What's the weather? | Assistant replied: 72 and sunny.\n"
    "[]\n"
    "They said: Text me that. | Assistant replied: The address is 5 Main St.\n"
    "[]   (a plain text-me in chat is already satisfied by the reply)")


def _build_user_prompt(user_message: str, assistant_message: str,
                       conversation_text: str, existing: list) -> str:
    parts = []
    if conversation_text:
        parts.append(f"Recent conversation:\n{conversation_text}\n")
    parts.append("This turn, verbatim:\n"
                 f"  They said: {user_message}\n"
                 f"  Assistant replied: {assistant_message}\n")
    if existing:
        parts.append("\nAlready-recorded pending items for this person (do not duplicate):")
        for c in existing[:6]:
            parts.append(f"\n- {(c.get('description') or '?')[:80]}")
    return "".join(parts)


def introspect_enabled() -> bool:
    from colony_sidecar.util.autonomy_preset import resolve_bool
    return resolve_bool("COLONY_INTROSPECT_ENABLED", False)


def _config() -> Dict[str, Any]:
    return {
        "base_url": os.environ.get("COLONY_INTROSPECT_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/"),
        "model": os.environ.get("COLONY_INTROSPECT_MODEL", "").strip(),
        "api_key": os.environ.get("COLONY_INTROSPECT_API_KEY", "").strip(),
        "timeout": float(os.environ.get("COLONY_INTROSPECT_TIMEOUT", "30")),
        "max_tokens": int(os.environ.get("COLONY_INTROSPECT_MAX_TOKENS", "400")),
    }




def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    """Pull the first JSON array out of the model's reply; [] on anything malformed."""
    if not text:
        return []
    try:
        chunk = text[text.index("["): text.rindex("]") + 1]
        data = json.loads(chunk)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:
        return []


async def run_turn_introspection(
    *,
    user_message: str,
    assistant_message: str,
    conversation_text: str,
    person_id: str,
    existing_commitments: Optional[List[Dict[str, Any]]],
    commitment_store: Any,
) -> Dict[str, Any]:
    """Judge one completed turn and record any owed follow-ups as commitments.

    Best-effort and exception-safe: a failure here never affects the turn. Returns a small
    dict describing what it did, mostly for tests/logging.
    """
    cfg = _config()
    if not cfg["model"] or commitment_store is None:
        return {"ok": False, "reason": "introspection not configured"}
    if not (user_message or assistant_message or conversation_text):
        return {"ok": False, "reason": "empty turn"}

    user_prompt = _build_user_prompt(
        user_message or "", assistant_message or "", conversation_text or "",
        existing_commitments or [])

    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            resp = await client.post(
                cfg["base_url"] + "/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key'] or 'x'}",
                         "Content-Type": "application/json"},
                json={
                    "model": cfg["model"], "temperature": 0.1, "max_tokens": cfg["max_tokens"],
                    "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                                 {"role": "user", "content": user_prompt}],
                },
            )
            resp.raise_for_status()
            content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        logger.warning("introspection LLM call failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

    items = _parse_json_array(content)
    created: List[str] = []
    for it in items:
        desc = str(it.get("description") or "").strip()
        if not desc:
            continue
        try:
            row = commitment_store.create(
                person_id=person_id,
                description=desc[:1000],
                due_at=(it.get("due_at") or None),
                priority=int(it.get("priority") or 60),
                source_type=(it.get("source_type") or "introspection"),
                source_context="inline turn introspection",
                metadata=(it.get("metadata") if isinstance(it.get("metadata"), dict) else None),
            )
            created.append(row.get("id"))
        except Exception as exc:
            # e.g. a non-future due_at is rejected by the store — skip that one, keep going.
            logger.debug("introspection commitment skipped: %s", exc)

    if created:
        logger.info("introspection recorded %d commitment(s) for %s", len(created), person_id)
    return {"ok": True, "created": created, "candidates": len(items)}
