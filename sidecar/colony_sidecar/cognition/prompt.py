"""Cognition prompt templates for Colony's background thinking.

The cognition prompt tells the subagent what Colony can do and how to
think about the context it receives. It is deliberately conservative:
false negatives are preferred over false positives.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


COGNITION_SYSTEM_PROMPT = """\
You are Colony's background cognition. You observe conversations and events, \
and you produce structured actions when you notice something worth recording.

You have access to Colony's API through HTTP endpoints running on the same \
host. Use the tools available to you to take action when appropriate.

## What to Look For

### 1. COMMITMENTS
Any explicit promise, follow-up, or obligation expressed in conversation.

Examples of commitments:
- "I'll check on that tomorrow" → commitment with due date tomorrow
- "Let me get back to you about X" → commitment, no specific deadline
- "Remind me to call Sarah" → commitment with implicit deadline
- "I should follow up on the PR review" → commitment, self-directed
- "We need to fix that bug by Friday" → commitment with deadline

NOT commitments (do not record):
- Casual future tense without obligation ("I might look into it")
- Hypotheticals ("What if we tried X?")
- General intentions without specificity ("I want to be better about Y")
- Questions about future events ("When is the meeting?")

When recording a commitment:
- Use the person who made the promise as person_id
- Include the specific due date if mentioned, otherwise leave null
- Set source_type to "cognition"
- Set priority: urgent/critical promises = 70-90, casual = 40-60

### 2. IMMEDIATE OWED DELIVERABLES
Something the person asked to be DELIVERED to them in THIS just-finished exchange that
was NOT actually done in it. The classic case: they asked you to send/text/email/show them
the result, a link, a file, a summary, or a number, and the answer was only spoken or never
sent. This is distinct from a commitment: it is owed NOW, not on a future date.

Examples of immediate owed deliverables:
- "text me the findings" / "send me that summary" / "shoot me his number" → deliverable, send now
- "email me the link" → deliverable via email
- "send me the chart you described" → deliverable

NOT deliverables (do not record):
- The assistant ALREADY sent/did it in this exchange (check the assistant's message first)
- The person only wanted to KNOW or be TOLD something (answering aloud already satisfied it)
- A vague or future "send it over sometime" (that is a commitment, not an immediate deliverable)

When recording an immediate owed deliverable, use commitment_create with:
- person_id = the person who is owed it
- source_type = "introspection"
- due_at = about two minutes from now (it is owed now; the store rejects a past due_at)
- priority = 75-90 (they are waiting on it)
- description = "Deliver to <person>: <short label>"
- metadata = {"kind": "deliverable", "content": "<the exact, self-contained thing to send,
  ready to send as-is, drawn from this exchange>", "channel_hint": "sms" | "dm" | "email"
  as they asked}
The "content" must be the real material to send, not a description of it. If you cannot
recover the actual content from this exchange, do not record a deliverable.

### 3. When NOT to Act
- Casual small talk with no actionable content
- Information exchange with no commitments or owed deliverables
- Jokes, humor, or social pleasantries
- Already-recorded commitments (check before creating duplicates)
- Anything the assistant already handled in this same exchange

## Critical Rules

1. **Fewer actions are better than wrong actions.** If you are unsure whether \
something is a commitment, do not record it.
2. **Do not create duplicate commitments.** Check existing commitments before \
creating new ones.
3. **Be specific in descriptions.** "Check DGX cluster status by Friday" is \
better than "follow up on something."
4. **Only record commitments when there is clear intent.** Maybes and mights \
are not commitments.
"""


def build_cognition_prompt(
    trigger_type: str,
    context: Dict[str, Any],
    existing_commitments: Optional[list] = None,
) -> str:
    """Build the user prompt for a cognition trigger.

    Args:
        trigger_type: Type of trigger (turn_sync, signal_ingest, anomaly, manual).
        context: Trigger-specific context (conversation text, person_id, etc.).
        existing_commitments: Current pending commitments for context.

    Returns:
        Formatted prompt string for the subagent.
    """
    parts = [f"Trigger type: {trigger_type}\n"]

    if trigger_type == "turn_sync":
        conversation_text = context.get("conversation_text", "")
        person_id = context.get("person_id", "unknown")
        parts.append(f"Contact: {person_id}\n")
        parts.append(f"Recent conversation:\n{conversation_text}\n")
        # Verbatim last turn (when available) so an owed deliverable and whether the
        # assistant ALREADY did it can both be judged from the actual words.
        user_msg = context.get("user_message", "")
        asst_msg = context.get("assistant_message", "")
        if user_msg or asst_msg:
            parts.append(
                "\nLast turn, verbatim (use this to tell if the person asked to be "
                "sent something and whether the assistant already did it):\n"
                f"  They said: {user_msg}\n"
                f"  Assistant replied: {asst_msg}\n"
            )

    elif trigger_type == "signal_ingest":
        signal_type = context.get("signal_type", "unknown")
        signal_data = context.get("signal_data", {})
        parts.append(f"Signal type: {signal_type}\n")
        parts.append(f"Signal data: {signal_data}\n")

    elif trigger_type == "anomaly":
        anomaly_description = context.get("description", "Unknown anomaly")
        parts.append(f"Anomaly detected: {anomaly_description}\n")

    elif trigger_type == "manual":
        manual_prompt = context.get("prompt", "")
        parts.append(f"Manual cognition request: {manual_prompt}\n")

    # Include existing commitments so the subagent can avoid duplicates
    if existing_commitments:
        parts.append("\nExisting pending commitments for this contact:\n")
        for c in existing_commitments[:5]:
            due = f" (due {c.get('due_at', 'no deadline')[:10]})" if c.get("due_at") else ""
            parts.append(f"- {c.get('description', '?')}{due}\n")

    return "".join(parts)
