#!/usr/bin/env python3
"""Poll Colony for pending initiatives and fire them to the Hermes webhook.

Install as a Hermes cron job:
    hermes cron create \
        --name colony-initiative-poller \
        --schedule "every 5m" \
        --script colony-initiative-poller.py \
        --no-agent

Environment variables:
    COLONY_URL          Colony sidecar URL (default: http://127.0.0.1:7777)
    COLONY_API_KEY      Colony API key (default: dev-mode-no-key)
    COLONY_HERMES_WEBHOOK_URL  Hermes webhook URL
"""

import json
import os
import urllib.request

COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "dev-mode-no-key")
WEBHOOK_URL = os.environ.get(
    "COLONY_HERMES_WEBHOOK_URL",
    "http://127.0.0.1:8644/webhooks/colony-initiatives",
)

SEEN_FILE = os.path.expanduser("~/.hermes/.colony_seen_initiatives")
DEDUP_FILE = os.path.expanduser("~/.hermes/.colony_seen_dedup")

# Platform-specific delivery targets (override via env vars)
USER_CHAT = os.environ.get("COLONY_USER_CHAT", "")
LOG_CHANNEL = os.environ.get("COLONY_LOG_CHANNEL", "")


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


def main():
    seen_ids = load_seen()
    seen_dedup = load_seen_dedup()

    headers = {"Authorization": f"Bearer {COLONY_API_KEY}"}
    req = urllib.request.Request(
        f"{COLONY_URL}/v1/host/initiatives",
        headers=headers,
        method="GET",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
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

        # Skip if we've already seen this exact ID
        if iid in seen_ids:
            skipped_seen += 1
            continue

        # Skip if we've already seen this dedup_key (prevents spam from
        # recurring initiatives like relationship reminders that get new IDs)
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
                "user_chat": USER_CHAT,
                "log_channel": LOG_CHANNEL,
                "platform": "",
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
            print(f"Fired: {iid} ({initiative.get('initiative_type')}) dedup={dedup_key}")
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
