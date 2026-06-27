"""Built-in Agent Bridge service -- auto-wired inside the sidecar.

Closes the autonomy circuit by forwarding pending initiatives and queued
jobs to the agent webhook. Runs as an async background task alongside the
autonomy loop, so it works on any platform with zero external setup.

This is the internal counterpart to the ``colony-agent-bridge`` console
script. The console script polls Colony over HTTP and is for split
deployments where agent and sidecar run on different machines. This
service accesses the initiative store and autonomy loop directly (no
HTTP round-trip to itself) and is the default for single-machine setups.

What it does every cycle (default 60s):
  - Drains pending initiatives from the store and POSTs them to the
    agent's initiative webhook
  - Claims queued agent_action jobs and POSTs them to the jobs webhook
  - Monitors the autonomy circuit for silent failures:
    * initiatives generated but never executed
    * autonomy loop stuck (ticks not advancing)
    * agent webhook unreachable
  - Syncs agent skills to the observation store (periodic, default 24h)

Auto-starts at sidecar boot when COLONY_BRIDGE_ENABLED is not "false"
and an agent webhook URL is configured. Disabled automatically when no
webhook is set (the agent hasn't connected yet).

Environment / config:
  COLONY_BRIDGE_ENABLED            "true" (default) / "false"
  COLONY_BRIDGE_WEBHOOK_URL        agent initiative webhook (required)
  COLONY_BRIDGE_JOBS_WEBHOOK_URL   agent jobs webhook (optional, same host)
  COLONY_BRIDGE_CYCLE_SECS         cycle interval (default 60)
  COLONY_BRIDGE_SKILLS_HOURS       skills sync interval (default 24)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AgentBridgeService:
    """Async service that bridges the sidecar's autonomy output to the agent."""

    def __init__(
        self,
        initiative_store: Any,
        autonomy_loop: Any = None,
        task_queue: Any = None,
        observation_store: Any = None,
        webhook_url: str = "",
        jobs_webhook_url: str = "",
        cycle_secs: float = 60.0,
        skills_sync_hours: float = 24.0,
        api_key: str = "",
        node_id: str = "sidecar-bridge",
    ):
        self._store = initiative_store
        self._autonomy = autonomy_loop
        self._queue = task_queue
        self._obs_store = observation_store
        self._webhook = webhook_url
        self._jobs_webhook = jobs_webhook_url or webhook_url.replace(
            "/colony-initiatives", "/colony-jobs"
        )
        self._cycle_secs = cycle_secs
        self._skills_hours = skills_sync_hours
        self._api_key = api_key
        self._node_id = node_id

        self._running = False
        self._stop_event = asyncio.Event()
        self._seen_ids: set = set()
        self._seen_dedup: set = set()
        self._last_skills_sync = 0.0
        self._last_ticks = 0
        self._consecutive_webhook_failures = 0
        self._stats = {
            "cycles": 0,
            "initiatives_forwarded": 0,
            "jobs_dispatched": 0,
            "webhook_failures": 0,
            "alerts_raised": 0,
        }

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._webhook:
            logger.info("Agent bridge: no webhook URL configured, staying dormant")
            return

        self._running = True
        self._stop_event.clear()
        logger.info(
            "Agent bridge starting (cycle=%ds, webhook=%s)",
            int(self._cycle_secs), self._webhook,
        )

        try:
            while not self._stop_event.is_set():
                await self._cycle()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._cycle_secs
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            logger.info("Agent bridge stopped. Stats: %s", self._stats)

    async def stop(self) -> None:
        logger.info("Agent bridge stop requested")
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def _cycle(self) -> None:
        self._stats["cycles"] += 1

        await self._forward_initiatives()
        await self._dispatch_jobs()
        self._check_circuit_health()
        await self._maybe_sync_skills()

    # ------------------------------------------------------------------
    # Initiative forwarding
    # ------------------------------------------------------------------

    async def _forward_initiatives(self) -> None:
        if self._store is None:
            return

        try:
            loop = asyncio.get_event_loop()
            pending = await loop.run_in_executor(
                None, lambda: self._store.list(status=["pending"], limit=50)
            )
        except Exception as exc:
            logger.warning("Failed to list pending initiatives: %s", exc)
            return

        for initiative in pending:
            iid = getattr(initiative, "id", "")
            dedup_key = getattr(initiative, "dedup_key", "") or ""

            if iid in self._seen_ids:
                continue
            if dedup_key and dedup_key in self._seen_dedup:
                self._seen_ids.add(iid)
                continue

            self._seen_ids.add(iid)
            if dedup_key:
                self._seen_dedup.add(dedup_key)

            payload = {
                "type": "initiative",
                "payload": {
                    "id": iid,
                    "initiative_type": getattr(initiative, "initiative_type", ""),
                    "title": getattr(initiative, "title", ""),
                    "description": getattr(initiative, "description", ""),
                    "priority": getattr(initiative, "priority", 0.5),
                    "status": "pending",
                    "entity_id": getattr(initiative, "entity_id", None),
                    "dedup_key": dedup_key,
                    "context": getattr(initiative, "context", None) or {},
                    "created_at": str(getattr(initiative, "created_at", "")),
                },
                "occurred_at": str(getattr(initiative, "created_at", "")),
            }

            ok = await self._post_webhook(self._webhook, payload)
            if ok:
                self._stats["initiatives_forwarded"] += 1
                logger.info(
                    "Forwarded initiative %s (%s)",
                    iid, getattr(initiative, "initiative_type", "?"),
                )

        self._rotate_seen_sets()

    # ------------------------------------------------------------------
    # Job dispatch
    # ------------------------------------------------------------------

    async def _dispatch_jobs(self) -> None:
        if self._queue is None:
            return

        try:
            loop = asyncio.get_event_loop()
            job = await loop.run_in_executor(
                None,
                lambda: self._queue.claim(
                    node_id=self._node_id, job_types=["agent_action"]
                ) if hasattr(self._queue, "claim") else None,
            )
        except Exception as exc:
            logger.debug("Job claim failed: %s", exc)
            return

        if not job:
            return

        job_id = job.get("job_id") or job.get("id", "")
        params = job.get("payload") or job.get("params") or {}

        colony_url = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
        payload = {
            "type": "agent_job",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "job_id": job_id,
                "action_hint": params.get("action_hint", ""),
                "domain": params.get("domain", ""),
                "risk": params.get("risk", ""),
                "description": params.get("description", ""),
                "context": params.get("context", {}),
                "colony_url": colony_url,
                "observations_url": f"{colony_url}/v1/host/observations",
                "complete_url": f"{colony_url}/v1/host/queue/jobs/{job_id}/complete",
                "fail_url": f"{colony_url}/v1/host/queue/jobs/{job_id}/fail",
                "api_key_header": "X-API-Key",
            },
        }

        ok = await self._post_webhook(self._jobs_webhook, payload)
        if ok:
            self._stats["jobs_dispatched"] += 1
            logger.info("Dispatched job %s (%s)", job_id, params.get("action_hint", "?"))

    # ------------------------------------------------------------------
    # Circuit health monitoring
    # ------------------------------------------------------------------

    def _check_circuit_health(self) -> None:
        if self._autonomy is None:
            return

        stats = getattr(self._autonomy, "stats", None)
        if stats is None:
            return

        ticks = getattr(stats, "ticks", 0)
        generated = getattr(stats, "initiatives_generated", 0)
        executed = getattr(stats, "actions_executed", 0)

        if self._last_ticks > 0 and ticks == self._last_ticks:
            self._log_alert(
                "autonomy_stuck",
                f"Autonomy loop stuck at tick {ticks}",
            )

        if generated > 100 and executed == 0 and self._stats["initiatives_forwarded"] == 0:
            self._log_alert(
                "initiatives_never_delivered",
                f"Generated {generated} initiatives but forwarded 0 to the agent. "
                f"Check webhook URL and agent availability.",
            )

        if self._consecutive_webhook_failures >= 5:
            self._log_alert(
                "webhook_unreachable",
                f"Agent webhook has failed {self._consecutive_webhook_failures} "
                f"consecutive times ({self._webhook})",
            )

        self._last_ticks = ticks

    def _log_alert(self, alert_type: str, message: str) -> None:
        self._stats["alerts_raised"] += 1
        logger.warning("[BRIDGE HEALTH] %s: %s", alert_type, message)

    # ------------------------------------------------------------------
    # Skills sync
    # ------------------------------------------------------------------

    async def _maybe_sync_skills(self) -> None:
        if self._obs_store is None:
            return

        import time as _time
        now = _time.monotonic()
        if self._last_skills_sync > 0 and (now - self._last_skills_sync) < (self._skills_hours * 3600):
            return
        self._last_skills_sync = now

        try:
            from colony_sidecar.workers.skills_sync import scan
            observations = scan()
            if not observations:
                return

            loop = asyncio.get_event_loop()
            for obs in observations:
                await loop.run_in_executor(
                    None,
                    lambda o=obs: self._obs_store.upsert(
                        domain="skills",
                        entity_id=o["entity_id"],
                        payload=o["payload"],
                        reported_by="agent-bridge",
                    ) if hasattr(self._obs_store, "upsert") else None,
                )
            logger.info("Skills sync: %d skills reported", len(observations))
        except ImportError:
            logger.debug("Skills sync: skills_sync module not available")
        except Exception as exc:
            logger.warning("Skills sync failed: %s", exc)

    # ------------------------------------------------------------------
    # Webhook helper
    # ------------------------------------------------------------------

    async def _post_webhook(self, url: str, payload: dict) -> bool:
        loop = asyncio.get_event_loop()
        try:
            def _do_post():
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload, default=str).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)

            await loop.run_in_executor(None, _do_post)
            self._consecutive_webhook_failures = 0
            return True
        except Exception as exc:
            self._consecutive_webhook_failures += 1
            self._stats["webhook_failures"] += 1
            logger.debug("Webhook POST failed (%s): %s", url, exc)
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rotate_seen_sets(self) -> None:
        if len(self._seen_ids) > 5000:
            self._seen_ids = set(sorted(self._seen_ids)[-2000:])
        if len(self._seen_dedup) > 5000:
            self._seen_dedup = set(sorted(self._seen_dedup)[-2000:])


# ---------------------------------------------------------------------------
# Factory + wiring
# ---------------------------------------------------------------------------

def create_from_env(
    initiative_store: Any = None,
    autonomy_loop: Any = None,
    task_queue: Any = None,
    observation_store: Any = None,
) -> Optional[AgentBridgeService]:
    """Create an AgentBridgeService from environment variables.

    Returns None if COLONY_BRIDGE_ENABLED is "false" or no webhook URL
    is configured.
    """
    if os.environ.get("COLONY_BRIDGE_ENABLED", "true").lower() == "false":
        logger.info("Agent bridge disabled (COLONY_BRIDGE_ENABLED=false)")
        return None

    webhook = os.environ.get("COLONY_BRIDGE_WEBHOOK_URL", "")
    if not webhook:
        logger.info(
            "Agent bridge: no COLONY_BRIDGE_WEBHOOK_URL set, will not start. "
            "Set it to the agent's initiative webhook to enable."
        )
        return None

    return AgentBridgeService(
        initiative_store=initiative_store,
        autonomy_loop=autonomy_loop,
        task_queue=task_queue,
        observation_store=observation_store,
        webhook_url=webhook,
        jobs_webhook_url=os.environ.get("COLONY_BRIDGE_JOBS_WEBHOOK_URL", ""),
        cycle_secs=float(os.environ.get("COLONY_BRIDGE_CYCLE_SECS", "60")),
        skills_sync_hours=float(os.environ.get("COLONY_BRIDGE_SKILLS_HOURS", "24")),
        api_key=os.environ.get("COLONY_API_KEY", ""),
        node_id=os.environ.get("COLONY_BRIDGE_NODE_ID", "sidecar-bridge"),
    )
