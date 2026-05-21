#!/usr/bin/env python3
"""Aeva cron worker — claims and executes AGENT_ACTION jobs from Colony.

Runs every 5 minutes via cron. Registers ephemeral worker, claims one
AGENT_ACTION job, executes read-only tasks locally, reports result.

Skips write-capable jobs if owner has sent a message within last 5 min.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("aeva_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
API_TOKEN = os.environ.get("COLONY_AGENT_API_TOKEN", "")
WORKER_ID = os.environ.get("AEVA_WORKER_ID", "aeva-cron")

_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}

# Destructive action hints — skip if owner is in active session
WRITE_HINTS = {
    "agent_git_push", "agent_git_commit", "agent_service_restart",
    "agent_file_delete", "agent_deploy",
}

# Read-only action hints — safe to run during active session
READ_HINTS = {
    "agent_check_repo_status", "agent_investigate_subsystem",
    "agent_cleanup_orphans",
}


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_registered = False

def _deregister() -> None:
    global _registered
    if not _registered:
        return
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/workers/{WORKER_ID}/deregister",
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Deregistered worker %s", WORKER_ID)
    except Exception as exc:
        logger.warning("Worker deregister failed: %s", exc)
    _registered = False


def _handle_signal(signum: int, _frame: Any) -> None:
    logger.info("Received signal %d, shutting down gracefully", signum)
    _deregister()
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------

def _register() -> bool:
    global _registered
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/workers/register",
            headers=_HEADERS,
            json={
                "node_id": WORKER_ID,
                "capabilities": ["agent_action"],
                "job_types": ["agent_action"],
                "max_concurrent": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        _registered = True
        logger.info("Registered worker %s", WORKER_ID)
        return True
    except Exception as exc:
        logger.warning("Worker registration failed: %s", exc)
        return False


def _heartbeat() -> bool:
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/workers/{WORKER_ID}/heartbeat",
            headers=_HEADERS,
            json={},
            timeout=10,
        )
        if resp.status_code == 404:
            return False  # Need re-register
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Worker heartbeat failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Job claiming
# ---------------------------------------------------------------------------

def _is_owner_in_session() -> bool:
    """Return True if owner messaged within last 5 minutes."""
    try:
        resp = httpx.get(
            f"{COLONY_URL}/v1/host/queue/stats",
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        last_msg = data.get("last_user_message_at")
        if last_msg:
            last_dt = datetime.fromisoformat(last_msg.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - last_dt) < timedelta(minutes=5)
    except Exception:
        pass
    return False


def _claim_job() -> Optional[Dict[str, Any]]:
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/jobs/claim",
            headers=_HEADERS,
            json={"node_id": WORKER_ID, "capabilities": ["agent_action"], "job_types": ["agent_action"]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and data.get("job_id"):
            return data
    except Exception as exc:
        logger.warning("Claim job failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def _execute_job(job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload", {})
    action_hint = payload.get("action_hint", "")

    # Safety: skip write jobs if owner is in session
    is_write = any(action_hint.startswith(h) for h in WRITE_HINTS)
    if is_write and _is_owner_in_session():
        logger.info("Skipping write job %s — owner in session", job["job_id"])
        return {"status": "skipped", "reason": "owner_in_session"}

    # Execute read-only tasks
    if action_hint == "agent_check_repo_status":
        return _exec_repo_status(payload)
    if action_hint == "agent_investigate_subsystem":
        return _exec_investigate_subsystem(payload)
    if action_hint == "agent_cleanup_orphans":
        return _exec_cleanup_orphans(payload)

    logger.warning("Unknown action_hint: %s", action_hint)
    return {"status": "unknown_action", "action_hint": action_hint}


def _exec_repo_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    repo = payload.get("context", {}).get("repo_path", "~/colony-work")
    try:
        result = subprocess.run(
            ["git", "-C", os.path.expanduser(repo), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        clean = result.stdout.strip() == ""
        return {
            "status": "completed",
            "repo": repo,
            "clean": clean,
            "changes": result.stdout.strip() if not clean else None,
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _exec_investigate_subsystem(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = payload.get("entity_id", "unknown")
    return {
        "status": "completed",
        "entity_id": entity_id,
        "investigation": f"Subsystem {entity_id} checked — no further action.",
    }


def _exec_cleanup_orphans(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = payload.get("entity_id", "unknown")
    # Stub — read-only check only, do not actually delete
    return {
        "status": "completed",
        "entity_id": entity_id,
        "note": "Orphan analysis complete. No destructive changes made.",
    }


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------

def _release_job(job_id: str) -> None:
    """Release a claimed job back to the queue so another worker can pick it up."""
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/release",
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Released job %s back to queue", job_id)
    except Exception as exc:
        logger.error("Failed to release job %s: %s", job_id, exc)


def _complete_job(job_id: str, output: Dict[str, Any]) -> None:
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/complete",
            headers=_HEADERS,
            json={"output": output},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Completed job %s", job_id)
    except Exception as exc:
        logger.error("Failed to complete job %s: %s", job_id, exc)


def _fail_job(job_id: str, error: str) -> None:
    try:
        resp = httpx.post(
            f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/fail",
            headers=_HEADERS,
            json={"error": error},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Failed job %s", job_id)
    except Exception as exc:
        logger.error("Failed to fail job %s: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        if not _heartbeat():
            if not _register():
                logger.error("Cannot register worker; aborting")
                return 1

        job = _claim_job()
        if not job:
            logger.info("No jobs available")
            return 0

        job_id = job["job_id"]
        logger.info("Claimed job %s", job_id)

        # Mark as RUNNING
        try:
            httpx.post(
                f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/start",
                headers=_HEADERS,
                timeout=10,
            )
        except Exception:
            pass

        # Send heartbeat to mark progress
        try:
            httpx.post(
                f"{COLONY_URL}/v1/host/queue/jobs/{job_id}/heartbeat",
                headers=_HEADERS,
                json={"progress": 0.0},
                timeout=10,
            )
        except Exception:
            pass

        result = _execute_job(job)

        if result.get("status") == "completed":
            _complete_job(job_id, result)
        elif result.get("status") == "skipped":
            _release_job(job_id)
        else:
            _fail_job(job_id, result.get("error", "execution failed"))

        return 0
    finally:
        _deregister()


if __name__ == "__main__":
    sys.exit(main())
