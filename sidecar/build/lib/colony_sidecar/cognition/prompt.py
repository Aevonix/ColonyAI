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

### 2. When NOT to Act
- Casual small talk with no actionable content
- Information exchange with no commitments
- Jokes, humor, or social pleasantries
- Already-recorded commitments (check before creating duplicates)

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
