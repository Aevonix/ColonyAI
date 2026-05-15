#!/usr/bin/env python3
"""Poll Colony for pending initiatives and fire them to the Hermes webhook.

v2 — temporal awareness, sync health, and auto-restart.

Phase 3 changes:
- Health preflight before fetching initiatives
- Service wake-up on connection failure (fire-and-forget)
- Wake-up flag state tracking to prevent infinite loops
- Alert payload for persistent failures (routed to log channel only)
- X-API-Key header for auth
- Last health response persisted to ~/.hermes/.colony_last_health
"""

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "dev-mode-no-key")
WEBHOOK_URL = "http://127.0.0.1:8644/webhooks/colony-initiatives"

SEEN_FILE = os.path.expanduser("~/.hermes/.colony_seen_initiatives")
DEDUP_FILE = os.path.expanduser("~/.hermes/.colony_seen_dedup")
LAST_HEALTH_FILE = os.path.expanduser("~/.hermes/.colony_last_health")
WAKE_UP_FLAG = os.path.expanduser("~/.hermes/.colony_wake_up_flag")

# Alert routing: logs only, never DM
LOG_CHANNEL = os.environ.get("COLONY_LOG_CHANNEL", "")
PLATFORM = os.environ.get("COLONY_PLATFORM", "whatsapp")


def load_seen() -> set:
    try:
        with open(SEEN_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def load_seen_dedup() -> set:
    try:
        with open(DEDUP_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        for iid in sorted(seen):
            f.write(iid + "\n")


def save_seen_dedup(seen: set) -> None:
    with open(DEDUP_FILE, "w") as f:
        for key in sorted(seen):
            f.write(key + "\n")


def attempt_wake_up() -> None:
    """Send launchctl start wake-up (fire-and-forget). Layer 1 (launchd) owns restart timing."""
    try:
        subprocess.run(
            ["launchctl", "start", "ai.aevonix.colony-sidecar"],
            capture_output=True,
            timeout=5,
        )
        print("Sent launchctl start wake-up to ai.aevonix.colony-sidecar")
    except Exception as exc:
        print(f"Wake-up failed: {exc}")


def fire_alert(last_health: dict) -> None:
    """Fire an alert to the webhook. Routed to log channel only."""
    payload = {
        "type": "alert",
        "payload": {
            "alert_type": "sidecar_down",
            "severity": "critical",
            "last_seen_at": last_health.get("last_seen_at", ""),
            "suggested_action": "Run: colony service start  or  launchctl load ~/Library/LaunchAgents/ai.aevonix.colony-sidecar.plist",
        },
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "seq": 0,
        "delivery_context": {
            "log_channel": LOG_CHANNEL,
            "platform": PLATFORM,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("Fired sidecar_down alert to log channel")
    except Exception as exc:
        print(f"Alert fire failed: {exc}")


def main():
    seen_ids = load_seen()
    seen_dedup = load_seen_dedup()

    # 1. Health check first
    headers = {"X-API-Key": COLONY_API_KEY}
    health_req = urllib.request.Request(
        f"{COLONY_URL}/v1/host/health",
        headers=headers,
        method="GET",
    )
    health_data = None
    try:
        resp = urllib.request.urlopen(health_req, timeout=10)
        health_data = json.loads(resp.read().decode("utf-8"))
        # Persist last health
        with open(LAST_HEALTH_FILE, "w") as f:
            json.dump(health_data, f, indent=2)
        # Remove wake-up flag on success
        if os.path.exists(WAKE_UP_FLAG):
            os.remove(WAKE_UP_FLAG)
    except Exception as exc:
        print(f"Health check failed: {exc}")
        # 2. Detect sidecar down — attempt wake-up
        if os.path.exists(WAKE_UP_FLAG):
            # 3. Wake-up was sent on previous cycle and sidecar is still down → fire alert
            last_health = {}
            try:
                with open(LAST_HEALTH_FILE) as f:
                    last_health = json.load(f)
            except Exception:
                pass
            fire_alert(last_health)
        else:
            # Send wake-up and create flag
            attempt_wake_up()
            Path(WAKE_UP_FLAG).touch()
        # Skip initiative fetching
        return

    # 4. Fetch initiatives
    init_req = urllib.request.Request(
        f"{COLONY_URL}/v1/host/initiatives",
        headers=headers,
        method="GET",
    )
    try:
        resp = urllib.request.urlopen(init_req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Failed to fetch initiatives: {exc}")
        return

    initiatives = data.get("initiatives", [])
    fired = 0
    skipped_dedup = 0
    skipped_seen = 0

    for initiative in initiatives:
        iid = initiative.get("id")
        status = initiative.get("status", "")
        dedup_key = initiative.get("dedup_key", "")

        if status != "pending":
            continue

        if iid in seen_ids:
            skipped_seen += 1
            continue

        if dedup_key and dedup_key in seen_dedup:
            seen_ids.add(iid)
            skipped_dedup += 1
            continue

        seen_ids.add(iid)
        if dedup_key:
            seen_dedup.add(dedup_key)

        payload = {
            "type": "initiative",
            "payload": initiative,
            "occurred_at": initiative.get("created_at", ""),
            "seq": 0,
            "delivery_context": {
                "log_channel": LOG_CHANNEL,
                "platform": PLATFORM,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        wh_req = urllib.request.Request(
            WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(wh_req, timeout=10)
            fired += 1
            print(f"Fired initiative: {iid} ({initiative.get('initiative_type')}) dedup={dedup_key}")
        except Exception as exc:
            print(f"Webhook fire failed for {iid}: {exc}")

    save_seen(seen_ids)
    save_seen_dedup(seen_dedup)

    if fired:
        print(f"Total fired: {fired}")
    if skipped_dedup:
        print(f"Skipped by dedup_key: {skipped_dedup}")
    if skipped_seen:
        print(f"Already seen (id): {skipped_seen}")
    if not fired and not skipped_dedup and not skipped_seen:
        print("No new pending initiatives.")


if __name__ == "__main__":
    main()
