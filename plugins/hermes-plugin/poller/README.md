# Colony Initiative Poller

Polls Colony for pending initiatives and fires them to the Hermes webhook.

## Installation

```bash
./install.sh --poller
```

Or manually copy `colony-initiative-poller.py` to `~/.hermes/scripts/`.

## Scheduling

Run every 60 seconds via Hermes cron:

```bash
hermes cron create \
    --name colony-initiative-poller \
    --schedule "every 1m" \
    --script colony-initiative-poller.py \
    --no-agent
```

Or use macOS `launchd` / Linux `cron`.

## Configuration

Environment variables:
- `COLONY_URL` — sidecar URL (default: `http://127.0.0.1:7777`)
- `COLONY_API_KEY` — API key (default: `dev-mode-no-key`)
- `COLONY_HERMES_WEBHOOK_URL` — webhook URL (default: `http://127.0.0.1:8644/webhooks/colony-initiatives`)
- `COLONY_LOG_CHANNEL` — log channel for alerts (optional)
- `COLONY_PLATFORM` — platform identifier (default: `whatsapp`)

## Features

- **Health preflight**: Checks `/v1/host/health` before fetching initiatives
- **Auto wake-up**: Sends `launchctl start` on connection failure
- **State tracking**: `~/.hermes/.colony_wake_up_flag` prevents infinite wake-up loops
- **Alert routing**: Fires `"alert"` payload to log channel only if wake-up fails twice
- **Deduplication**: Skips initiatives by `dedup_key` to prevent spam
- **Auth**: Uses `X-API-Key` header

## Queue Worker (v0.16.0 — agent-as-sensor)

`colony-queue-worker.py` is the execution half of the loop: it claims
`agent_action` jobs from Colony's task queue (including the read-only
`agent_sync_<domain>` observation requests) and fires them to the
`colony-jobs` webhook route, where the agent executes them with its own
toolsets and closes the lifecycle via curl (report observations →
complete/fail the job).

```bash
hermes cron create \
    --name colony-queue-worker \
    --schedule "every 5m" \
    --script colony-queue-worker.py \
    --no-agent
```

Requires the `colony-jobs` route from `examples/webhook-config.yaml` in
`~/.hermes/config.yaml`. Env vars: `COLONY_URL`, `COLONY_API_KEY`,
`COLONY_JOBS_WEBHOOK_URL`, `COLONY_WORKER_NODE_ID`,
`COLONY_WORKER_MAX_JOBS` (default 1 job per run).
