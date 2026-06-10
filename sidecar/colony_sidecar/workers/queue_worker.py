"""Claim Colony agent_action jobs and hand them to the agent.

Installed as the ``colony-queue-worker`` console script (v0.20.0); the
logic moved here unchanged from
``plugins/hermes-plugin/poller/colony-queue-worker.py`` (v0.16.0),
which remains as a thin back-compat wrapper.

This is the execution half of the agent-as-sensor loop:

  Colony notices a domain is stale → posts a read-only agent_sync_<domain>
  job → THIS worker claims it and fires it to the agent webhook → the
  agent observes through its own toolsets (github, terminal, web, ...) →
  the agent POSTs observations back to Colony and completes the job →
  Colony generates initiatives from what the agent saw.

The same path carries every other registered agent_action capability.
Run from cron every few minutes (the wizard installs ``*/5 * * * *``).
Without it, auto-approved agent_action jobs sit QUEUED forever.

The webhook prompt receives explicit lifecycle URLs so the agent can
close the loop with plain curl — no special tooling needed:

  - report observations:  POST {COLONY_URL}/v1/host/observations
  - complete the job:     POST {COLONY_URL}/v1/host/queue/jobs/{id}/complete
  - fail the job:         POST {COLONY_URL}/v1/host/queue/jobs/{id}/fail

Environment (unchanged from the v0.16 script):
  COLONY_URL                sidecar URL       (default http://127.0.0.1:7777)
  COLONY_API_KEY            API key           (default dev-mode-no-key)
  COLONY_JOBS_WEBHOOK_URL   agent webhook     (default http://127.0.0.1:8644/webhooks/colony-jobs)
  COLONY_WORKER_NODE_ID     worker identity   (default derived from COLONY_AGENT_NAME)
  COLONY_WORKER_MAX_JOBS    jobs per run      (default 1 — each is a full agent invocation)

Stdlib-only on purpose — must run from cron without the sidecar deps.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def load_config() -> Dict[str, Any]:
    """Resolve worker configuration from the environment.

    Read at call time (not import time) so cron env sourcing and tests
    both behave predictably.
    """
    node_id = os.environ.get("COLONY_WORKER_NODE_ID") or (
        os.environ.get("COLONY_AGENT_NAME", "hermes").strip().lower().replace(" ", "-")
        + "-agent"
    )
    return {
        "colony_url": os.environ.get("COLONY_URL", "http://127.0.0.1:7777"),
        "api_key": os.environ.get("COLONY_API_KEY", "dev-mode-no-key"),
        "webhook_url": os.environ.get(
            "COLONY_JOBS_WEBHOOK_URL", "http://127.0.0.1:8644/webhooks/colony-jobs"
        ),
        "node_id": node_id,
        # How many jobs to hand to the agent per run. Keep small — each
        # one is a full agent invocation.
        "max_jobs": int(os.environ.get("COLONY_WORKER_MAX_JOBS", "1")),
    }


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {"X-API-Key": cfg["api_key"], "Content-Type": "application/json"}


def _post(cfg: Dict[str, Any], url: str, body: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=_headers(cfg), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def register_worker(cfg: Dict[str, Any]) -> None:
    """Registration is ephemeral (lost on sidecar restart) — re-register every run."""
    _post(
        cfg,
        f"{cfg['colony_url']}/v1/host/queue/workers/register",
        {
            "node_id": cfg["node_id"],
            "capabilities": ["shell", "filesystem", "web_search", "git"],
            "job_types": ["agent_action"],
            "max_concurrent": 1,
            "available": True,
            "load": 0.0,
        },
    )


def claim_job(cfg: Dict[str, Any]) -> Optional[dict]:
    return _post(
        cfg,
        f"{cfg['colony_url']}/v1/host/queue/jobs/claim",
        {"node_id": cfg["node_id"], "job_types": ["agent_action"]},
    ) or None


def build_webhook_payload(cfg: Dict[str, Any], job: dict) -> dict:
    """Render the agent-webhook payload for a claimed job (pure — testable)."""
    job_id = job.get("job_id") or job.get("id")
    params = job.get("payload") or job.get("params") or {}
    colony_url = cfg["colony_url"]
    return {
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


def fire_to_agent(cfg: Dict[str, Any], job: dict) -> bool:
    """Hand the claimed job to the agent via the jobs webhook.

    The webhook route's prompt template renders these fields; the agent
    decides how to execute with its own toolsets and closes the job
    lifecycle itself via the URLs in the payload.
    """
    job_id = job.get("job_id") or job.get("id")
    payload = build_webhook_payload(cfg, job)
    try:
        req = urllib.request.Request(
            cfg["webhook_url"],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as exc:
        print(f"Webhook fire failed for job {job_id}: {exc}")
        # Release the claim so another run can retry
        try:
            _post(cfg, f"{cfg['colony_url']}/v1/host/queue/jobs/{job_id}/release", {})
        except Exception:
            pass
        return False


def run(cfg: Dict[str, Any]) -> int:
    """One cron tick: register, claim up to max_jobs, hand each to the agent."""
    try:
        register_worker(cfg)
    except Exception as exc:
        print(f"Worker registration failed (sidecar down?): {exc}")
        return 0

    handed_off = 0
    for _ in range(cfg["max_jobs"]):
        try:
            job = claim_job(cfg)
        except Exception as exc:
            print(f"Claim failed: {exc}")
            break
        if not job:
            break
        job_id = job.get("job_id") or job.get("id")
        hint = (job.get("payload") or {}).get("action_hint", "?")
        if fire_to_agent(cfg, job):
            handed_off += 1
            print(f"Handed job {job_id} ({hint}) to the agent")

    if not handed_off:
        print("No pending agent_action jobs.")
    return handed_off


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="colony-queue-worker",
        description="Claim Colony agent_action jobs and hand them to the agent webhook.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved configuration and exit without any network calls",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.dry_run:
        print("colony-queue-worker (dry run — no network calls):")
        print(f"  colony_url:  {cfg['colony_url']}")
        print(f"  webhook_url: {cfg['webhook_url']}")
        print(f"  node_id:     {cfg['node_id']}")
        print(f"  max_jobs:    {cfg['max_jobs']}")
        return 0

    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
