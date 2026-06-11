# Colony ⇄ Hermes integration ops add-ons (generic, host-side)

These are **generic** add-ons that make a Colony cognitive sidecar robust on a
Hermes agent host — not specific to any single agent. They live host-side under
`~/.hermes/` (which survives Hermes updates) and are the building blocks the
Colony setup wizard installs/validates.

| File | Installs to | Purpose |
|------|-------------|---------|
| `colony-doctor.py` | `~/.hermes/scripts/` | Validates the deployed Hermes↔Colony integration. STATIC: every plugin's registered hook names are checked against the running Hermes build's `VALID_HOOKS`; hook callbacks must be **sync** (Hermes invokes them as `cb(**kwargs)` and drops un-awaited coroutines) and accept `**kwargs`; tool handlers must absorb `**kwargs`; flags `ctx.config` / `register_slash_command`. LIVE: sidecar reachable, contact-resolve answers, Colony LLM is a generative (non-embedding) provider. Exits non-zero on any failure. |
| `colony-doctor-cron.sh` | `~/.hermes/scripts/` | Runs the doctor; alerts the WhatsApp **home/ops channel** only on a regression **or a Hermes version change** (the doctor records the last-seen version in `~/.hermes/.colony_doctor_state.json`). |
| `ai.aevonix.colony-doctor.plist` | `~/Library/LaunchAgents/` | launchd job: runs the doctor at load + every 6h. |
| `hermes-gateway-restart-runner.sh` | `~/.hermes/scripts/` | Robust gateway restart (bootout-wait + bootstrap-retry + bridge-wait). Writes a pre-restart resume marker and posts the restart notice to the **home/ops channel** (never the owner's main chat). |
| `pre-restart-summary.py` | `~/.hermes/scripts/` | Captures the last exchange + recent (deduped) tools + Colony timeline digest into `~/.hermes/.post_restart_resume` so the agent resumes context after a restart. |

## Why the doctor exists

Hermes invokes plugin hooks **synchronously** as `cb(**kwargs)` and injects any
returned `{"context": str}` into the user turn. Several failure modes are
**silent** — no error, the hook just never fires:

- an `async def` hook → returns an un-awaited coroutine that is dropped
- a hook registered under a name not in this build's `VALID_HOOKS` (e.g.
  `agent:start`) → dropped with only a one-line WARNING at load
- a hook/tool handler whose signature can't absorb the kwargs Hermes passes
  (`sender_id`, `task_id`, …) → raises and is swallowed

The doctor catches all of these statically, and re-runs automatically when the
Hermes version changes — so a Hermes upgrade that shifts hook conventions is
surfaced immediately instead of silently degrading Colony.

## Install

```bash
cp colony-doctor.py colony-doctor-cron.sh hermes-gateway-restart-runner.sh \
   pre-restart-summary.py ~/.hermes/scripts/
cp ai.aevonix.colony-doctor.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.aevonix.colony-doctor.plist
~/.hermes/scripts/colony-doctor.py   # run once
```

Override the plugins dir for testing: `COLONY_DOCTOR_PLUGINS_DIR=/path ./colony-doctor.py`.
