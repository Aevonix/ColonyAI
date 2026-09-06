"""Durable checkpoints using the same outbox and delivery path as turns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .client import ColonyClient, TurnOutbox


def direct_evidence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep direct content and media references, never injected API context."""
    if len(messages) > 20_000:
        raise ValueError("checkpoint exceeds the message limit")
    result = []
    for message in messages:
        if not isinstance(message, dict) or message.get("_compressed_summary"):
            continue
        if message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                continue
        elif isinstance(content, list):
            if not content or not all(isinstance(block, dict) for block in content):
                raise ValueError("checkpoint contains unsupported content blocks")
        elif content is None:
            continue
        else:
            raise ValueError("checkpoint contains unsupported message content")
        result.append({"role": message["role"], "content": content})
    return result


def checkpoint(
    messages: list[dict[str, Any]], *, session_id: str, contact_id: str,
    home: Path, url: str, api_key: str, outbox_path: str = "",
) -> dict[str, Any]:
    evidence = direct_evidence(messages)
    if not evidence:
        return {"state": "empty", "messages": 0}
    if not session_id or not contact_id:
        raise ValueError("checkpoint requires a bound session and participant")
    payload = {
        "session_id": session_id, "contact_id": contact_id,
        "checkpoint_messages": evidence,
    }
    digest = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode()).hexdigest()
    turn_id = "checkpoint:" + digest
    payload["turn_id"] = turn_id
    outbox = TurnOutbox(outbox_path or home / "state" / "colony-turn-outbox.sqlite3")
    # The local commit is the checkpoint guarantee. Oversize, full queue or
    # failed storage raises before Hermes compresses; no text is truncated.
    receipt = outbox.enqueue(turn_id, payload)
    state = receipt["state"]
    if state == "pending":
        client = ColonyClient(url=url, api_key=api_key)
        try:
            outbox.drain(
                lambda stored, *, timeout_seconds: client.sync_turn(
                    **stored, outbox=outbox, timeout_seconds=timeout_seconds,
                ),
                limit=16, timeout_seconds=0.25,
            )
            state = outbox.enqueue(turn_id, payload)["state"]
        except Exception:
            # The ordinary general-adapter drain or a later checkpoint replays
            # this committed row. Pending does not mean centrally recallable.
            state = "pending"
    return {"state": state, "messages": len(evidence), "turn_id": turn_id}
