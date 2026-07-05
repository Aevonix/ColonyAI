"""Training-corpus exporter: turn store -> fine-tune-ready JSONL.

Row shape (the DeepSpec / standard fine-tune contract, one JSON object per
line): {"id": ..., "conversations": [{"role": "user"|"assistant", "content":
...}, ...], "meta": {...}}. Conversations always start with a user message
and alternate user/assistant, so they parse under GeneralParser-style
tooling without warnings.

PII stance: everything stays local. Source rows never leave the sidecar's
SQLite; the export file is always written under COLONY_STATE_DIR/exports/
(path traversal is not accepted). Optional redact=true masks credential /
token shapes via the sidecar's redact machinery. Consumers (fine-tune
pipelines) read the file from the state dir; nothing is uploaded by this
code.

Quality gate: system/skill-origin turns are excluded using the same
sanitize/origin machinery the reachout policy uses (is_system_origin +
sanitize_text), machine markers (e.g. reply-context [[rc ...]] suffixes)
are stripped, cron/self sessions are excluded by default, and exact
duplicate exchanges are deduplicated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from colony_sidecar.mining.store import MiningStore

logger = logging.getLogger(__name__)

_RC_MARKER = re.compile(r"\s*\[\[rc [^\]]*\]\]")


def _parse_when(value: Optional[str]) -> Optional[float]:
    """ISO timestamp or relative '<n>d'/'<n>h' -> epoch seconds."""
    if not value:
        return None
    v = value.strip()
    m = re.fullmatch(r"(\d+)([dh])", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return time.time() - n * (86400 if unit == "d" else 3600)
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ValueError(f"unparseable time filter: {value!r}")


def _clean(text: str, redact: bool) -> str:
    out = _RC_MARKER.sub("", text or "").strip()
    try:
        from colony_sidecar.delivery.reachout_policy import sanitize_text

        out = sanitize_text(out)
    except Exception:
        pass
    if redact:
        try:
            from colony_sidecar.redact import redact_sensitive_text

            out = redact_sensitive_text(out)
        except Exception:
            pass
    return out.strip()


def _system_origin(text: str) -> bool:
    try:
        from colony_sidecar.delivery.reachout_policy import is_system_origin

        return is_system_origin(text or "")
    except Exception:
        return False


def export_corpus(
    store: MiningStore,
    *,
    state_dir: Path,
    contact_id: Optional[str] = None,
    channels: Optional[List[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    group: str = "turn",
    min_chars: int = 2,
    include_cron: bool = False,
    include_escalations: bool = False,
    dedup: bool = True,
    redact: bool = False,
    limit: int = 100000,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Export filtered conversations as JSONL; returns stats + path."""
    import os

    if group not in ("turn", "session"):
        raise ValueError("group must be 'turn' or 'session'")
    if contact_id is None:
        contact_id = os.environ.get("COLONY_OWNER_CONTACT_ID", "owner")

    turns = store.list_turns(
        contact_id=contact_id,
        channels=channels,
        since_ts=_parse_when(since),
        until_ts=_parse_when(until),
        limit=limit,
    )

    skipped = {"quality": 0, "cron": 0, "dedup": 0}
    seen: set = set()
    exchanges: List[Dict[str, Any]] = []
    for t in turns:
        if not include_cron and t.session_id.startswith("cron_"):
            skipped["cron"] += 1
            continue
        user = _clean(t.user_text, redact)
        asst = _clean(t.assistant_text, redact)
        if (
            len(user) < min_chars
            or len(asst) < min_chars
            or _system_origin(t.user_text)
            or _system_origin(t.assistant_text)
        ):
            skipped["quality"] += 1
            continue
        if dedup:
            h = hashlib.sha256(f"{user}\x00{asst}".encode()).hexdigest()
            if h in seen:
                skipped["dedup"] += 1
                continue
            seen.add(h)
        exchanges.append(
            {
                "session_id": t.session_id,
                "user": user,
                "assistant": asst,
                "meta": {
                    "channel": t.channel_id,
                    "contact": t.contact_id,
                    "session": t.session_id,
                    "ts": t.ts,
                    "model": t.model,
                },
            }
        )

    rows: List[Dict[str, Any]] = []
    if group == "turn":
        for i, ex in enumerate(exchanges):
            rows.append(
                {
                    "id": f"turn-{i}",
                    "conversations": [
                        {"role": "user", "content": ex["user"]},
                        {"role": "assistant", "content": ex["assistant"]},
                    ],
                    "meta": ex["meta"],
                }
            )
    else:
        by_session: Dict[str, List[Dict[str, Any]]] = {}
        for ex in exchanges:
            by_session.setdefault(ex["session_id"], []).append(ex)
        for i, (sid, exs) in enumerate(by_session.items()):
            conv: List[Dict[str, str]] = []
            for ex in exs:
                conv.append({"role": "user", "content": ex["user"]})
                conv.append({"role": "assistant", "content": ex["assistant"]})
            rows.append(
                {
                    "id": f"session-{i}",
                    "conversations": conv,
                    "meta": {**exs[0]["meta"], "exchanges": len(exs)},
                }
            )

    escalations_included = 0
    if include_escalations:
        for e in store.list_escalations(limit=1000):
            user = _clean(e.task_context, redact)
            asst = _clean(e.escalated_answer, redact)
            if len(user) < min_chars or len(asst) < min_chars:
                continue
            rows.append(
                {
                    "id": f"escalation-{e.id[:8]}",
                    "conversations": [
                        {"role": "user", "content": user},
                        {"role": "assistant", "content": asst},
                    ],
                    "meta": {
                        "kind": e.kind,
                        "channel": e.channel_id,
                        "matched": e.matched,
                        "outcome": e.outcome,
                        "model": e.model,
                        "ts": e.ts,
                    },
                }
            )
            escalations_included += 1

    exports_dir = Path(state_dir) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    name = filename or f"corpus-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    name = Path(name).name  # no traversal: basename only, always under exports/
    out_path = exports_dir / name
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    stats = {
        "path": str(out_path),
        "rows": len(rows),
        "exchanges": len(exchanges),
        "sessions": len({ex["session_id"] for ex in exchanges}),
        "turns_scanned": len(turns),
        "skipped": skipped,
        "escalations_included": escalations_included,
        "group": group,
        "contact_id": contact_id,
        "redacted": redact,
    }
    try:
        from colony_sidecar.events.journal import append_event

        append_event("mining.corpus_export", stats)
    except Exception:
        logger.debug("corpus export journal failed", exc_info=True)
    logger.info(
        "corpus export: %d rows (%d sessions) -> %s", len(rows), stats["sessions"], out_path
    )
    return stats
