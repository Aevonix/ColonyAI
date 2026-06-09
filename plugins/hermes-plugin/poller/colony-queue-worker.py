#!/usr/bin/env python3
"""Claim Colony agent_action jobs and hand them to the agent (v0.16.0).

This is the execution half of the agent-as-sensor loop:

  Colony notices a domain is stale → posts a read-only agent_sync_<domain>
  job → THIS script claims it and fires it to the Hermes webhook → the
  agent observes through its own toolsets (github, terminal, web, ...) →
  the agent POSTs observations back to Colony and completes the job →
  Colony generates initiatives from what the agent saw.

The same path carries every other registered agent_action capability.
Run from cron every few minutes, after colony-initiative-poller.py.

The webhook prompt receives explicit lifecycle URLs so the agent can
close the loop with plain curl — no special tooling needed:

  - report observations:  POST {COLONY_URL}/v1/host/observations
  - complete the job:     POST {COLONY_URL}/v1/host/queue/jobs/{id}/complete
  - fail the job:         POST {COLONY_URL}/v1/host/queue/jobs/{id}/fail
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "dev-mode-no-key")
WEBHOOK_URL = os.environ.get(
    "COLONY_JOBS_WEBHOOK_URL", "http://127.0.0.1:8644/webhooks/colony-jobs"
)
# Worker identity is deployment-specific: set COLONY_WORKER_NODE_ID, or
# COLONY_AGENT_NAME (the agent's name) from which a node id is derived.
NODE_ID = os.environ.get("COLONY_WORKER_NODE_ID") or (
    os.environ.get("COLONY_AGENT_NAME", "hermes").strip().lower().replace(" ", "-")
    + "-agent"
)
# How many jobs to hand to the agent per run. Keep small — each one is
# a full agent invocation.
MAX_JOBS_PER_RUN = int(os.environ.get("COLONY_WORKER_MAX_JOBS", "1"))

HEADERS = {"X-API-Key": COLONY_API_KEY, "Content-Type": "application/json"}


def _post(url: str, body: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=HEADERS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def register_worker() -> None:
    """Registration is ephemeral (lost on sidecar restart) — re-register every run."""
    _post(
        f"{COLONY_URL}/v1/host/queue/workers/register",
        {
            "node_id": NODE_ID,
            "capabilities": ["shell", "filesystem", "web_search", "git"],
            "job_types": ["agent_action"],
            "max_concurrent": 1,
            "available": True,
            "load": 0.0,
        },
    )


def claim_job() -> dict | None:
    return _post(
        f"{COLONY_URL}/v1/host/queue/jobs/claim",
        {"node_id": NODE_ID, "job_types": ["agent_action"]},
    ) or None


def fire_to_agent(job: dict) -> bool:
    """Hand the claimed job to the agent via the Hermes webhook.

    The webhook route's prompt template renders these fields; the agent
    decides how to execute with its own toolsets and closes the job
    lifecycle itself via the URLs below.
    """
    job_id = job.get("job_id") or job.get("id")
    params = job.get("payload") or job.get("params") or {}
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
            "colony_url": COLONY_URL,
            "observations_url": f"{COLONY_URL}/v1/host/observations",
            "complete_url": f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/complete",
            "fail_url": f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/fail",
            "api_key_header": "X-API-Key",
        },
    }
    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
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
            _post(f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/release", {})
        except Exception:
            pass
        return False


def main() -> None:
    try:
        register_worker()
    except Exception as exc:
        print(f"Worker registration failed (sidecar down?): {exc}")
        return

    handed_off = 0
    for _ in range(MAX_JOBS_PER_RUN):
        try:
            job = claim_job()
        except Exception as exc:
            print(f"Claim failed: {exc}")
            break
        if not job:
            break
        job_id = job.get("job_id") or job.get("id")
        hint = (job.get("payload") or {}).get("action_hint", "?")
        if fire_to_agent(job):
            handed_off += 1
            print(f"Handed job {job_id} ({hint}) to the agent")

    if not handed_off:
        print("No pending agent_action jobs.")


if __name__ == "__main__":
    main()
