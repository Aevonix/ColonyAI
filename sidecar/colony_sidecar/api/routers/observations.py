"""Observation ingestion API (v0.16.0).

The agent reports what it sees through its own Hermes connections;
Colony's initiative generators read from here. This is the inbound
half of the agent-as-sensor loop — the outbound half is the read-only
``agent_sync_<domain>`` jobs the autonomy loop posts to the task queue
when a domain goes stale.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from colony_sidecar.observations.store import OBSERVATION_DOMAINS, ObservationStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/host/observations", tags=["observations"])

_observation_store: Optional[ObservationStore] = None


def set_observation_store(store: Optional[ObservationStore]) -> None:
    global _observation_store
    _observation_store = store


def get_observation_store() -> Optional[ObservationStore]:
    return _observation_store


class ObservationIn(BaseModel):
    entity_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    observed_at: Optional[str] = None


class ObservationBatchRequest(BaseModel):
    domain: str
    reported_by: Optional[str] = None
    observations: List[ObservationIn] = Field(default_factory=list)


def _require_store() -> ObservationStore:
    if _observation_store is None:
        raise HTTPException(status_code=501, detail="Observation store not initialized")
    return _observation_store


def _validate_domain(domain: str) -> str:
    if domain not in OBSERVATION_DOMAINS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown domain {domain!r}; expected one of {sorted(OBSERVATION_DOMAINS)}",
        )
    return domain


@router.post("")
async def ingest_observations(body: ObservationBatchRequest) -> Dict[str, Any]:
    """Record a batch of domain snapshots reported by an agent."""
    store = _require_store()
    _validate_domain(body.domain)
    written = store.record_batch(
        domain=body.domain,
        observations=[o.model_dump() for o in body.observations],
        reported_by=body.reported_by,
    )
    logger.info(
        "Recorded %d observation(s) for domain %s (reported_by=%s)",
        written, body.domain, body.reported_by,
    )
    return {"status": "recorded", "domain": body.domain, "written": written}


@router.get("")
async def observation_summary() -> Dict[str, Any]:
    """Per-domain counts and freshness — the agent's sensor health."""
    store = _require_store()
    return {"domains": store.summary(), "known_domains": list(OBSERVATION_DOMAINS)}


@router.get("/{domain}")
async def list_observations(
    domain: str,
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    store = _require_store()
    _validate_domain(domain)
    observations = store.list(domain, limit=limit)
    return {
        "domain": domain,
        "observations": [o.to_dict() for o in observations],
        "total": len(observations),
    }


@router.delete("/{domain}/{entity_id}")
async def delete_observation(domain: str, entity_id: str) -> Dict[str, Any]:
    """Remove one entity's snapshot (e.g. a closed PR the agent retired)."""
    store = _require_store()
    _validate_domain(domain)
    deleted = store.delete(domain, entity_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Observation not found")
    return {"status": "deleted", "domain": domain, "entity_id": entity_id}
