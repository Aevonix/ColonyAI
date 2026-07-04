"""Generic JSON-pull connector (read-only): GETs a metrics/business endpoint
and normalizes selected fields into "metrics" observations + a Project/Product
entity carrying those metrics.

This is a PULL connector (the sidecar fetches); push-style ingress belongs to
the host framework's webhook adapter and is deliberately not built here. Config
env-only (COLONY_CONNECTOR_WEBHOOK_*): URL, AUTH_HEADER + AUTH_VALUE, FIELD_MAP
(JSON dotted-path map), ID_FIELD, ENTITY_NAME, ENTITY_KIND.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from colony_sidecar.connectors.base import Connector, EntityHint, Observation

logger = logging.getLogger(__name__)


def _dig(obj: Any, path: str) -> Any:
    """Resolve a dotted path (a.b.0.c) against nested dict/list JSON."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


class WebhookPullConnector(Connector):
    name = "webhook"
    domain = "metrics"
    default_poll_secs = 3600

    def normalize(self, data: Any, *, field_map: Dict[str, str],
                  entity_name: str, entity_kind: str = "project",
                  id_field: str = "") -> List[Observation]:
        """Pure: map configured fields out of a JSON body into one metrics obs."""
        metrics = {out_key: _dig(data, src) for out_key, src in field_map.items()}
        metrics = {k: v for k, v in metrics.items() if v is not None}
        ext_id = str(_dig(data, id_field) if id_field else entity_name) or entity_name
        kv = ", ".join(f"{k}={v}" for k, v in metrics.items())
        text = f"Metrics for {entity_name}: {kv}" if kv else f"Metrics for {entity_name}"
        entities = [EntityHint(kind=entity_kind, name=entity_name)] if entity_name else []
        return [Observation(
            domain=self.domain, external_id=str(ext_id)[:200], ts=time.time(),
            payload={"entity": entity_name, "metrics": metrics},
            entities=entities, text=text)]

    def _fetch(self) -> Any:
        import urllib.request
        url = self.config.get("URL")
        if not url:
            return None
        req = urllib.request.Request(url, method="GET")
        header = self.config.get("AUTH_HEADER")
        value = self.config.get("AUTH_VALUE")
        if header and value:
            req.add_header(header, value)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def poll(self) -> List[Observation]:
        try:
            data = self._fetch()
            if data is None:
                return []
            try:
                field_map = json.loads(self.config.get("FIELD_MAP", "{}"))
            except json.JSONDecodeError:
                field_map = {}
            return self.normalize(
                data, field_map=field_map or {},
                entity_name=self.config.get("ENTITY_NAME", "metrics"),
                entity_kind=self.config.get("ENTITY_KIND", "project"),
                id_field=self.config.get("ID_FIELD", ""))
        except Exception:
            logger.debug("webhook pull failed", exc_info=True)
            return []
