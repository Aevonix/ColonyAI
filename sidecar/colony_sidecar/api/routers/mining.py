"""Mining API: escalation records + training-corpus export.

GET  /v1/host/mining/escalations        recent records + stats
POST /v1/host/mining/corpus/export      filtered JSONL export (stays local)

Auth rides the global ApiKeyMiddleware like every other router.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from colony_sidecar.mining.corpus import export_corpus
from colony_sidecar.mining.escalations import EscalationMiner
from colony_sidecar.mining.models import corpus_export_enabled, mining_mode
from colony_sidecar.mining.store import MiningStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/host/mining", tags=["mining"])

_mining_store: Optional[MiningStore] = None
_mining_engine: Optional[EscalationMiner] = None
_mining_state_dir: Optional[Path] = None


def set_mining(
    store: Optional[MiningStore],
    engine: Optional[EscalationMiner],
    state_dir: Optional[Path] = None,
) -> None:
    global _mining_store, _mining_engine, _mining_state_dir
    _mining_store = store
    _mining_engine = engine
    _mining_state_dir = state_dir


def get_mining_engine() -> Optional[EscalationMiner]:
    return _mining_engine


def get_mining_store() -> Optional[MiningStore]:
    return _mining_store


class CorpusExportRequest(BaseModel):
    contact_id: Optional[str] = None            # default: owner contact ("*" = all)
    channels: Optional[List[str]] = None
    since: Optional[str] = None                 # ISO or '7d' / '24h'
    until: Optional[str] = None
    group: str = "turn"                         # 'turn' | 'session'
    min_chars: int = Field(default=2, ge=1)
    include_cron: bool = False
    include_escalations: bool = False
    dedup: bool = True
    redact: bool = False
    limit: int = Field(default=100000, ge=1, le=1000000)
    filename: Optional[str] = None


@router.get("/escalations")
def list_escalations(
    kind: Optional[str] = Query(None, description="consultation | provider_escalation"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    if _mining_engine is None:
        raise HTTPException(status_code=501, detail="Mining not initialized")
    return {
        "mode": mining_mode(),
        "stats": _mining_engine.stats(),
        "escalations": _mining_engine.recent(kind=kind, limit=limit),
    }


@router.post("/corpus/export")
def corpus_export(body: CorpusExportRequest) -> Dict[str, Any]:
    if _mining_store is None or _mining_state_dir is None:
        raise HTTPException(status_code=501, detail="Mining not initialized")
    if not corpus_export_enabled():
        raise HTTPException(status_code=403, detail="Corpus export disabled (COLONY_CORPUS_EXPORT_ENABLED)")
    try:
        return export_corpus(
            _mining_store,
            state_dir=_mining_state_dir,
            contact_id=body.contact_id,
            channels=body.channels,
            since=body.since,
            until=body.until,
            group=body.group,
            min_chars=body.min_chars,
            include_cron=body.include_cron,
            include_escalations=body.include_escalations,
            dedup=body.dedup,
            redact=body.redact,
            limit=body.limit,
            filename=body.filename,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
