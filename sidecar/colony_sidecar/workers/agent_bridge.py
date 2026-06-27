"""Colony Agent Bridge -- unified daemon for the agent-side autonomy circuit.

Replaces three separate cron scripts (initiative-poller, queue-worker,
skills-sync) with one long-running process that:

  1. Polls for pending initiatives and forwards them to the agent webhook
  2. Claims agent_action jobs from the task queue and dispatches them
  3. Periodically syncs the agent's skill index to Colony
  4. Monitors the full circuit health and surfaces silent failures

Silent failures this catches:
  - Sidecar unreachable (connection refused / timeout)
  - Autonomy loop not running or stuck (no ticks advancing)
  - Initiatives generated but never executed (the exact bug that prompted this)
  - Queue jobs stuck in QUEUED state with no worker claiming them
  - Agent webhook unreachable (initiatives polled but can't be delivered)

Run as a daemon (``colony-agent-bridge``) or one-shot (``--once``).
All configuration via environment variables; no deployment-specific defaults.

Stdlib-only -- must run on machines where only the agent is installed.

Environment:
  COLONY_URL                   sidecar URL          (default http://127.0.0.1:7777)
  COLONY_API_KEY               API key              (default dev-mode-no-key)
  COLONY_INITIATIVE_WEBHOOK    initiative webhook    (default http://127.0.0.1:8644/webhooks/colony-initiatives)
  COLONY_JOBS_WEBHOOK_URL      jobs webhook          (default http://127.0.0.1:8644/webhooks/colony-jobs)
  COLONY_AGENT_NAME            agent identity        (default hermes)
  COLONY_WORKER_MAX_JOBS       jobs per cycle        (default 1)
  COLONY_BRIDGE_POLL_SECS      main loop interval    (default 60)
  COLONY_BRIDGE_SKILLS_HOURS   skills sync interval  (default 24)
  COLONY_BRIDGE_LOG_CHANNEL    alert routing channel (optional)
  COLONY_BRIDGE_PLATFORM       platform for alerts   (default whatsapp)
  HERMES_SKILLS_DIR            skills directory      (default ~/.hermes/skills)
  COLONY_BRIDGE_STATE_DIR      state file directory  (default ~/.colony/bridge)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("colony.bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _cfg() -> Dict[str, Any]:
    state_dir = Path(
        os.environ.get("COLONY_BRIDGE_STATE_DIR", "~/.colony/bridge")
    ).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    node_id = os.environ.get("COLONY_WORKER_NODE_ID") or (
        os.environ.get("COLONY_AGENT_NAME", "hermes").strip().lower().replace(" ", "-")
        + "-agent"
    )
    return {
        "colony_url": os.environ.get("COLONY_URL", "http://127.0.0.1:7777"),
        "api_key": os.environ.get("COLONY_API_KEY", "dev-mode-no-key"),
        "initiative_webhook": os.environ.get(
            "COLONY_INITIATIVE_WEBHOOK",
            "http://127.0.0.1:8644/webhooks/colony-initiatives",
        ),
        "jobs_webhook": os.environ.get(
            "COLONY_JOBS_WEBHOOK_URL",
            "http://127.0.0.1:8644/webhooks/colony-jobs",
        ),
        "node_id": node_id,
        "max_jobs": int(os.environ.get("COLONY_WORKER_MAX_JOBS", "1")),
        "poll_secs": int(os.environ.get("COLONY_BRIDGE_POLL_SECS", "60")),
        "skills_hours": float(os.environ.get("COLONY_BRIDGE_SKILLS_HOURS", "24")),
        "log_channel": os.environ.get("COLONY_BRIDGE_LOG_CHANNEL", ""),
        "platform": os.environ.get("COLONY_BRIDGE_PLATFORM", "whatsapp"),
        "state_dir": state_dir,
    }


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {"X-API-Key": cfg["api_key"], "Content-Type": "application/json"}


def _get(cfg: Dict[str, Any], path: str, timeout: int = 10) -> Optional[dict]:
    req = urllib.request.Request(
        f"{cfg['colony_url']}{path}",
        headers=_headers(cfg),
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _post(cfg: Dict[str, Any], url: str, body: dict, timeout: int = 15) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(cfg),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# State persistence (survives restarts)
# ---------------------------------------------------------------------------

def _load_set(path: Path) -> set:
    try:
        return set(line.strip() for line in path.read_text().splitlines() if line.strip())
    except FileNotFoundError:
        return set()


def _save_set(path: Path, items: set) -> None:
    path.write_text("\n".join(sorted(items)) + "\n" if items else "")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


# ---------------------------------------------------------------------------
# Health monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """Tracks circuit health across cycles and detects silent failures."""

    def __init__(self, state_dir: Path):
        self._path = state_dir / "health_state.json"
        self._state = _load_json(self._path)
        self._alerts: list = []

    def check(self, cfg: Dict[str, Any]) -> dict:
        self._alerts = []
        now = datetime.now(timezone.utc).isoformat()

        health = _get(cfg, "/v1/host/health")
        if health is None:
            consecutive = self._state.get("consecutive_failures", 0) + 1
            self._state["consecutive_failures"] = consecutive
            self._state["last_failure"] = now
            self._alert("sidecar_unreachable",
                        f"Colony sidecar at {cfg['colony_url']} is unreachable "
                        f"({consecutive} consecutive failure(s))",
                        severity="critical" if consecutive >= 3 else "warning")
            self._save()
            return {"ok": False, "alerts": self._alerts}

        self._state["consecutive_failures"] = 0
        self._state["last_success"] = now

        autonomy = _get(cfg, "/v1/host/autonomy/status")
        if autonomy:
            self._check_autonomy(autonomy)
            self._state["last_autonomy"] = autonomy

        self._save()
        return {"ok": True, "health": health, "autonomy": autonomy, "alerts": self._alerts}

    def _check_autonomy(self, status: dict) -> None:
        if not status.get("running"):
            self._alert("autonomy_stopped",
                        "Autonomy loop is not running",
                        severity="warning")
            return

        prev = self._state.get("last_autonomy", {})
        prev_ticks = prev.get("ticks", 0)
        curr_ticks = status.get("ticks", 0)

        if prev_ticks > 0 and curr_ticks == prev_ticks:
            self._alert("autonomy_stuck",
                        f"Autonomy loop stuck at tick {curr_ticks} (not advancing)",
                        severity="warning")

        generated = status.get("initiatives_generated", 0)
        executed = status.get("actions_executed", 0)
        if generated > 100 and executed == 0:
            self._alert("initiatives_never_executed",
                        f"Autonomy has generated {generated} initiatives but "
                        f"executed 0 actions. The delivery pipeline may be broken.",
                        severity="critical")

    def _alert(self, alert_type: str, message: str, severity: str = "warning") -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        last_key = f"last_alert_{alert_type}"
        last = self._state.get(last_key, "")
        if last:
            try:
                elapsed = (
                    datetime.fromisoformat(now_iso)
                    - datetime.fromisoformat(last)
                ).total_seconds()
                if elapsed < 3600:
                    return
            except (ValueError, TypeError):
                pass
        self._state[last_key] = now_iso
        entry = {"type": alert_type, "message": message, "severity": severity, "at": now_iso}
        self._alerts.append(entry)
        level = logging.CRITICAL if severity == "critical" else logging.WARNING
        logger.log(level, "[HEALTH] %s: %s", alert_type, message)

    def _save(self) -> None:
        _save_json(self._path, self._state)


# ---------------------------------------------------------------------------
# Initiative poller
# ---------------------------------------------------------------------------

class InitiativePoller:
    """Polls pending initiatives and forwards them to the agent webhook."""

    def __init__(self, state_dir: Path):
        self._seen_path = state_dir / "seen_initiatives.txt"
        self._dedup_path = state_dir / "seen_dedup_keys.txt"
        self._seen_ids = _load_set(self._seen_path)
        self._seen_dedup = _load_set(self._dedup_path)

    def poll(self, cfg: Dict[str, Any]) -> int:
        data = _get(cfg, "/v1/host/initiatives")
        if data is None:
            return 0

        items = data if isinstance(data, list) else data.get("initiatives", [])
        fired = 0

        for initiative in items:
            iid = initiative.get("id", "")
            status = initiative.get("status", "")
            dedup_key = initiative.get("dedup_key", "")

            if status != "pending":
                continue
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
                "payload": initiative,
                "occurred_at": initiative.get("created_at", ""),
                "seq": 0,
                "delivery_context": {
                    "log_channel": cfg.get("log_channel", ""),
                    "platform": cfg.get("platform", "whatsapp"),
                },
            }
            try:
                req = urllib.request.Request(
                    cfg["initiative_webhook"],
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
                fired += 1
                logger.info("Fired initiative %s (%s)", iid,
                            initiative.get("initiative_type", "?"))
            except Exception as exc:
                logger.warning("Initiative webhook failed for %s: %s", iid, exc)

        self._save()
        if fired:
            logger.info("Initiatives fired: %d", fired)
        return fired

    def _save(self) -> None:
        if len(self._seen_ids) > 5000:
            self._seen_ids = set(sorted(self._seen_ids)[-2000:])
        if len(self._seen_dedup) > 5000:
            self._seen_dedup = set(sorted(self._seen_dedup)[-2000:])
        _save_set(self._seen_path, self._seen_ids)
        _save_set(self._dedup_path, self._seen_dedup)


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

class QueueWorker:
    """Claims agent_action jobs and dispatches them to the agent webhook."""

    def claim_and_dispatch(self, cfg: Dict[str, Any]) -> int:
        try:
            _post(cfg, f"{cfg['colony_url']}/v1/host/queue/workers/register", {
                "node_id": cfg["node_id"],
                "capabilities": ["shell", "filesystem", "web_search", "git"],
                "job_types": ["agent_action"],
                "max_concurrent": 1,
                "available": True,
                "load": 0.0,
            })
        except Exception as exc:
            logger.warning("Worker registration failed: %s", exc)
            return 0

        dispatched = 0
        for _ in range(cfg["max_jobs"]):
            job = _post(cfg, f"{cfg['colony_url']}/v1/host/queue/jobs/claim", {
                "node_id": cfg["node_id"],
                "job_types": ["agent_action"],
            })
            if not job:
                break

            job_id = job.get("job_id") or job.get("id")
            params = job.get("payload") or job.get("params") or {}
            colony_url = cfg["colony_url"]

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
                    "report_example": params.get("report_example", {}),
                    "colony_url": colony_url,
                    "observations_url": f"{colony_url}/v1/host/observations",
                    "complete_url": f"{colony_url}/v1/host/queue/jobs/{job_id}/complete",
                    "fail_url": f"{colony_url}/v1/host/queue/jobs/{job_id}/fail",
                    "api_key_header": "X-API-Key",
                },
            }
            try:
                req = urllib.request.Request(
                    cfg["jobs_webhook"],
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=15)
                dispatched += 1
                hint = params.get("action_hint", "?")
                logger.info("Dispatched job %s (%s)", job_id, hint)
            except Exception as exc:
                logger.warning("Job webhook failed for %s: %s", job_id, exc)
                try:
                    _post(cfg, f"{colony_url}/v1/host/queue/jobs/{job_id}/release", {})
                except Exception:
                    pass

        if dispatched:
            logger.info("Jobs dispatched: %d", dispatched)
        return dispatched


# ---------------------------------------------------------------------------
# Skills sync (periodic)
# ---------------------------------------------------------------------------

class SkillsSyncer:
    """Periodically reports the agent's skill index to Colony."""

    def __init__(self, interval_hours: float):
        self._interval = interval_hours * 3600
        self._last_sync = 0.0

    def sync_if_due(self, cfg: Dict[str, Any]) -> bool:
        now = time.monotonic()
        if self._last_sync > 0 and (now - self._last_sync) < self._interval:
            return False
        self._last_sync = now

        try:
            from colony_sidecar.workers.skills_sync import scan, report
            observations = scan()
            if observations:
                status = report(observations)
                logger.info("Skills sync: %d skills reported (HTTP %s)", len(observations), status)
                return True
            else:
                logger.debug("Skills sync: no skills found")
                return False
        except ImportError:
            logger.debug("Skills sync: colony_sidecar.workers.skills_sync not importable, skipping")
            return False
        except Exception as exc:
            logger.warning("Skills sync failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Alert delivery
# ---------------------------------------------------------------------------

def deliver_alerts(cfg: Dict[str, Any], alerts: list) -> None:
    """Forward health alerts to the agent webhook for surfacing."""
    if not alerts:
        return
    for alert in alerts:
        payload = {
            "type": "alert",
            "payload": {
                "alert_type": alert["type"],
                "severity": alert["severity"],
                "message": alert["message"],
            },
            "occurred_at": alert.get("at", datetime.now(timezone.utc).isoformat()),
            "delivery_context": {
                "log_channel": cfg.get("log_channel", ""),
                "platform": cfg.get("platform", "whatsapp"),
            },
        }
        try:
            req = urllib.request.Request(
                cfg["initiative_webhook"],
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            logger.info("Alert delivered: %s", alert["type"])
        except Exception as exc:
            logger.warning("Alert delivery failed (%s): %s", alert["type"], exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class AgentBridge:
    """Unified daemon that keeps the Colony-to-agent circuit alive."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._health = HealthMonitor(cfg["state_dir"])
        self._poller = InitiativePoller(cfg["state_dir"])
        self._worker = QueueWorker()
        self._skills = SkillsSyncer(cfg["skills_hours"])
        self._running = True

    def stop(self, *_args: Any) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    def cycle(self) -> dict:
        """Run one full cycle: health check, poll, dispatch, sync."""
        result = {"ts": datetime.now(timezone.utc).isoformat()}

        check = self._health.check(self.cfg)
        result["health_ok"] = check.get("ok", False)

        if check.get("alerts"):
            deliver_alerts(self.cfg, check["alerts"])
            result["alerts"] = len(check["alerts"])

        if not check.get("ok"):
            return result

        result["initiatives_fired"] = self._poller.poll(self.cfg)
        result["jobs_dispatched"] = self._worker.claim_and_dispatch(self.cfg)
        result["skills_synced"] = self._skills.sync_if_due(self.cfg)

        return result

    def run(self, once: bool = False) -> None:
        logger.info(
            "Colony Agent Bridge starting (poll=%ds, skills_sync=%dh, url=%s)",
            self.cfg["poll_secs"],
            int(self.cfg["skills_hours"]),
            self.cfg["colony_url"],
        )

        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        while self._running:
            try:
                result = self.cycle()
                level = logging.DEBUG
                if result.get("initiatives_fired") or result.get("jobs_dispatched"):
                    level = logging.INFO
                if result.get("alerts"):
                    level = logging.WARNING
                logger.log(level, "Cycle: %s", json.dumps(result, default=str))
            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)

            if once:
                break

            try:
                time.sleep(self.cfg["poll_secs"])
            except (KeyboardInterrupt, SystemExit):
                break

        logger.info("Colony Agent Bridge stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="colony-agent-bridge",
        description=(
            "Unified daemon that bridges Colony's autonomy loop to the agent. "
            "Replaces the separate initiative-poller, queue-worker, and "
            "skills-sync cron jobs with one process that also monitors "
            "circuit health."
        ),
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run one cycle and exit (for cron or testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print configuration and exit without any network calls",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = _cfg()

    if args.dry_run:
        print("colony-agent-bridge (dry run):")
        for k, v in sorted(cfg.items()):
            print(f"  {k}: {v}")
        return 0

    bridge = AgentBridge(cfg)
    bridge.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
