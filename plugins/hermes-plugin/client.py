"""Sidecar client wrapper for Colony general plugin."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class ColonyClient:
    """Sync + async HTTP client for Colony sidecar."""

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = url or os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
        self._api_key = api_key or os.environ.get("COLONY_API_KEY", "")
        self._async_client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> dict[str, str]:
        h = {}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def get(self, path: str, **kwargs) -> httpx.Response:
        with httpx.Client(timeout=kwargs.pop("timeout", 5)) as client:
            return client.get(f"{self.url}{path}", headers=self._headers(), **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        with httpx.Client(timeout=kwargs.pop("timeout", 5)) as client:
            return client.post(f"{self.url}{path}", headers=self._headers(), **kwargs)

    async def aget(self, path: str, **kwargs) -> httpx.Response:
        client = self._get_async_client()
        return await client.get(f"{self.url}{path}", headers=self._headers(), **kwargs)

    async def apost(self, path: str, **kwargs) -> httpx.Response:
        client = self._get_async_client()
        return await client.post(f"{self.url}{path}", headers=self._headers(), **kwargs)

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient()
        return self._async_client

    async def close(self) -> None:
        if self._async_client and not self._async_client.is_closed:
            await self._async_client.aclose()
            self._async_client = None

    def health(self) -> dict[str, Any]:
        try:
            resp = self.get("/v1/host/health", timeout=3)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)}

    def list_goals(self, status: str = "active") -> List[dict]:
        try:
            resp = self.get("/v1/host/goals", params={"status_filter": status}, timeout=5)
            resp.raise_for_status()
            return resp.json().get("goals", [])
        except Exception as exc:
            logger.debug("list_goals failed: %s", exc)
            return []

    def get_briefings(self) -> List[dict]:
        try:
            resp = self.get("/v1/host/briefings", timeout=5)
            resp.raise_for_status()
            return resp.json().get("briefings", [])
        except Exception as exc:
            logger.debug("get_briefings failed: %s", exc)
            return []

    def list_initiatives(self, status: Optional[str] = None, limit: int = 50) -> List[dict]:
        try:
            # NOTE: The API status filter is unreliable (store expects list,
            # router passes string). Fetch all and filter client-side.
            resp = self.get("/v1/host/initiatives", params={"limit": limit}, timeout=5)
            resp.raise_for_status()
            initiatives = resp.json().get("initiatives", [])
            if status:
                initiatives = [i for i in initiatives if i.get("status") == status]
            return initiatives
        except Exception as exc:
            logger.debug("list_initiatives failed: %s", exc)
            return []

    def get_initiative(self, initiative_id: str) -> Optional[dict]:
        try:
            resp = self.get(f"/v1/host/initiatives/{initiative_id}", timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("get_initiative failed: %s", exc)
            return None

    def trigger_autonomy_cycle(self) -> dict:
        try:
            resp = self.post("/v1/host/autonomy/cycle", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("trigger_autonomy_cycle failed: %s", exc)
            return {"completed": False, "error": str(exc)}

    def assemble_context(self, query: str, contact_id: str, session_id: str = "") -> dict:
        try:
            resp = self.post(
                "/v1/host/context/assemble",
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": session_id,
                        "contact_id": contact_id,
                    },
                    "incoming_message": {"role": "user", "content": query},
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("assemble_context failed: %s", exc)
            return {"sections": []}

    def ingest_signals(self, signals: List[dict], contact_id: str, session_id: str = "") -> bool:
        try:
            resp = self.post(
                "/v1/host/signals/ingest",
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": session_id,
                        "contact_id": contact_id,
                    },
                    "signals": signals,
                },
                timeout=5,
            )
            return resp.status_code < 300
        except Exception as exc:
            logger.debug("ingest_signals failed: %s", exc)
            return False

    def replay_events(self, since: str, limit: int = 500, types: Optional[List[str]] = None) -> List[dict]:
        try:
            params: dict[str, Any] = {"since": since, "limit": limit}
            if types:
                params["types"] = ",".join(types)
            resp = self.get("/v1/host/events/replay", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json().get("events", [])
        except Exception as exc:
            logger.debug("replay_events failed: %s", exc)
            return []
