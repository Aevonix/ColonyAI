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
