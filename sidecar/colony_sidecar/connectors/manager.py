"""ConnectorManager -- registers pull connectors and runs the ingest phase
(cognition item 2, Phase C).

Per due connector: poll -> for each Observation, OBSERVE-boundary check the
subject (a "don't look at X" blackout suppresses ingest of X) -> record to the
observation store (feeds the initiative engine) -> feed the world-model
populator (which upserts entities/relationships under its OWN shadow-first
gate; belief maintenance rides the populator's inline property-audit hook, so
changed facts are reconciled without a second pass here).

Mode (COLONY_CONNECTORS_MODE, default off):
  off    -> no polling.
  shadow -> CALIBRATION: poll + normalize + LOG the output and the entities it
            WOULD populate, writing nothing (inspect a source before trusting
            it, same discipline as the world-model populator shadow).
  live   -> record observations + feed the populator.

Per-connector cadence and enable/credentials are env-only
(COLONY_CONNECTOR_<NAME>_*). Default off; enable per connector.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from colony_sidecar.connectors.base import Connector, Observation

logger = logging.getLogger(__name__)


def connectors_enabled() -> bool:
    return os.environ.get(
        "COLONY_CONNECTORS_ENABLED", "false").strip().lower() in (
            "1", "true", "yes", "on")


def connectors_mode() -> str:
    m = os.environ.get("COLONY_CONNECTORS_MODE", "off").strip().lower()
    return m if m in ("off", "shadow", "live") else "off"


class ConnectorManager:
    def __init__(self, *, observation_store: Any = None, populator: Any = None,
                 directive_manager: Any = None, self_model: Any = None) -> None:
        self._obs = observation_store
        self._populator = populator
        self._directives = directive_manager
        self._self_model = self_model
        self._connectors: List[Connector] = []
        self._last: Dict[str, Dict[str, Any]] = {}

    # -- registration -----------------------------------------------------
    def register(self, connector: Connector) -> None:
        self._connectors.append(connector)

    def register_default_connectors(self) -> int:
        """Register every reference connector whose env-enable flag is set."""
        from colony_sidecar.connectors.imap_email import IMAPEmailConnector
        from colony_sidecar.connectors.caldav_calendar import CalendarConnector
        from colony_sidecar.connectors.fs_documents import FSDocumentsConnector
        from colony_sidecar.connectors.webhook_pull import WebhookPullConnector
        for cls in (IMAPEmailConnector, CalendarConnector,
                    FSDocumentsConnector, WebhookPullConnector):
            try:
                c = cls()
                if c.enabled:
                    self.register(c)
            except Exception:
                logger.debug("connector %s init failed", cls.__name__,
                             exc_info=True)
        return len(self._connectors)

    # -- boundary gate ----------------------------------------------------
    def _boundary_ok(self, obs: Observation) -> bool:
        """OBSERVE-capability check: a subject under a perception blackout is
        suppressed. Reads survive an ACT-level "leave X alone"."""
        if self._directives is None:
            return True
        try:
            from colony_sidecar.directives import Action
            subject = " ".join([e.name for e in obs.entities]
                               + [str(obs.payload.get("subject", ""))]).strip()
            return self._directives.check(Action(
                kind="observe", text=subject or obs.domain,
                target=subject)).allowed
        except Exception:
            return True

    # -- the ingest phase -------------------------------------------------
    async def poll_due(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now if now is not None else time.time()
        mode = connectors_mode()
        report: Dict[str, Any] = {"mode": mode, "connectors": [],
                                  "observations": 0, "populated": 0,
                                  "skipped_boundary": 0}
        if mode == "off" or not self._connectors:
            return report

        for c in self._connectors:
            if not c.enabled or not c.due(now):
                continue
            try:
                observations = c.poll()
            except Exception:
                logger.debug("connector %s poll failed", c.name, exc_info=True)
                observations = []
            c.mark_polled(now)

            kept: List[Observation] = []
            for obs in observations:
                if not self._boundary_ok(obs):
                    report["skipped_boundary"] += 1
                    continue
                kept.append(obs)

            counts = await self._ingest(c, kept, mode)
            self._last[c.name] = {
                "at": now, "domain": c.domain, "count": len(kept),
                "populated": counts["populated"]}
            report["connectors"].append({
                "name": c.name, "domain": c.domain,
                "observations": len(kept), **counts})
            report["observations"] += len(kept)
            report["populated"] += counts["populated"]

        if report["observations"] or report["skipped_boundary"]:
            self._journal(report)
        return report

    async def _ingest(self, connector: Connector, observations: List[Observation],
                      mode: str) -> Dict[str, int]:
        populated = 0
        if mode == "shadow":
            # Log the normalized output + would-populate entities; write nothing.
            for obs in observations:
                logger.info("connector[shadow] %s: %s id=%s entities=%s",
                            connector.name, obs.domain, obs.external_id,
                            [e.name for e in obs.entities][:6])
            return {"recorded": 0, "populated": 0}

        # live: record observations, then feed the populator.
        recorded = 0
        if self._obs is not None and observations:
            try:
                rows = [o.to_store_row() for o in observations]
                recorded = self._obs.record_batch(
                    connector.domain, rows, reported_by=f"connector:{connector.name}")
            except Exception:
                logger.debug("observation record failed", exc_info=True)
        if self._populator is not None:
            for obs in observations:
                if not obs.text:
                    continue
                try:
                    await self._populator.populate_from_text(
                        obs.text, source_id=f"connector:{connector.name}:{obs.external_id}")
                    populated += 1
                except Exception:
                    logger.debug("populate failed", exc_info=True)
        return {"recorded": recorded, "populated": populated}

    # -- observability ----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "mode": connectors_mode(),
            "enabled": connectors_enabled(),
            "connectors": [
                {"name": c.name, "domain": c.domain, "enabled": c.enabled,
                 "poll_secs": c.poll_secs, "last": self._last.get(c.name)}
                for c in self._connectors],
        }

    def _journal(self, report: Dict[str, Any]) -> None:
        journal = getattr(self._self_model, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "connectors",
                f"ingested {report['observations']} observation(s) from "
                f"{len(report['connectors'])} connector(s)",
                reasoning=f"mode={report['mode']}, "
                          f"skipped_boundary={report['skipped_boundary']}",
                decision="acted" if report["mode"] == "live" else "held",
                reversibility="reversible")
        except Exception:
            logger.debug("connector journal failed", exc_info=True)
