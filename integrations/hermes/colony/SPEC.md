# Colony ↔ Hermes Autonomy Bridge — Implementation Spec

**Goal:** Enable Hermes to act on Colony initiatives autonomously via a cron job, with wizard setup and manual fallback.

---

## 1. ARCHITECTURE

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────┐
│  Colony     │────▶│  Cron Job    │────▶│  Hermes Agent       │
│  Sidecar    │     │  (every 15m) │     │  (autonomous run)   │
│  :7777      │     │              │     │                     │
└─────────────┘     └──────────────┘     └─────────────────────┘
       │                                           │
       │                                           │
       ▼                                           ▼
  Initiatives DB                            WhatsApp / SMS
  (7 relationship)                          (draft or send)
```

The cron job is an **LLM-driven agent** (not script-only) so it can reason about context, check briefings, and make judgment calls.

---

## 2. COMPONENTS TO BUILD

### 2.1 Plugin Tool: `colony_list_initiatives`

- **New tool** in `~/.hermes/plugins/colony/__init__.py`
- Calls `GET /v1/host/initiatives` (no `status=all` — no filter = all)
- Returns: `{initiatives: [...], total: N}`
- Also add `colony_get_initiative(id)` for fetching a single initiative with full context

### 2.2 Plugin Tools: `colony_send_message` (relationship action)

- **New tool** that wraps the existing send_message capability
- Allows the agent to send a drafted message to a contact
- Requires explicit contact resolution from the initiative's `entity_id`

### 2.3 Slash Commands

Add to `slash.py`:
- `/colony autonomy enable` — creates the cron job, triggers first cycle
- `/colony autonomy disable` — pauses/removes the cron job
- `/colony autonomy status` — shows last run, pending initiatives count, actions taken

### 2.4 Cron Job

**Job spec:**
```yaml
name: "Colony Autonomy Bridge"
schedule: "every 15m"
deliver: origin  # Report back to user only when action taken
enabled_toolsets: ["web", "terminal", "send_message", "file"]
```

**Prompt (self-contained):**
```
You are the Colony Autonomy Bridge — an agent that acts on behalf of the owner
by consuming initiatives from the Colony sidecar.

Your job each cycle:
1. Query Colony for pending initiatives via colony_list_initiatives
2. For each initiative, classify its type:
   - RELATIONSHIP: the owner hasn't contacted someone in a while.
     → Fetch their briefing via colony_get_briefing
     → Draft a warm, context-aware message IN THE OWNER'S VOICE
     → SEND IT DIRECTLY TO THE CONTACT via send_message
     → Only skip if contact channel is unknown or content feels wrong
   - FOLLOW_UP / TASK: A goal needs action.
     → Attempt to complete it with available tools
     → If blocked, report why
   - SCHEDULING: A commitment is due.
     → Draft a scheduling suggestion
     → Deliver to user
3. After handling all initiatives, report ONLY:
   - Actions taken autonomously (messages sent, tasks completed, etc.)
   - Items that need human judgment (with your reasoning)
   - Any errors

Stay silent ([SILENT]) if there are no initiatives and nothing to report.
NEVER send reminders TO the owner. Either act for them, or report that you couldn't.
```

### 2.5 Setup / Wizard Integration

**Path A (Wizard):**
During `colony setup` or plugin enablement, prompt:
> "Enable autonomous initiative handling? Colony will check for relationship
> reminders and tasks every 15 minutes, acting on your behalf when confident. [Y/n]"

If yes:
1. Call `cron.jobs.create_job()` programmatically
2. Trigger first cycle via `POST /v1/host/autonomy/cycle`
3. Print confirmation

**Path B (Manual):**
Always print post-setup:
> "Colony is ready. Run `/colony autonomy enable` to activate background
> initiative handling, or `hermes colony autonomy enable` from CLI."

---

## 3. FILE CHANGES

### `~/.hermes/plugins/colony/__init__.py`
- Add `colony_list_initiatives` and `colony_get_initiative` to `_TOOL_SCHEMAS`
- Add handlers `_handle_colony_list_initiatives`, `_handle_colony_get_initiative`
- Add `colony_autonomy_enable`, `colony_autonomy_disable`, `colony_autonomy_status` tools
- Add `_create_autonomy_job()` helper using `cron.jobs.create_job`

### `~/.hermes/plugins/colony/client.py`
- Add `list_initiatives()` method
- Add `get_initiative(id)` method
- Add `trigger_autonomy_cycle()` method

### `~/.hermes/plugins/colony/slash.py`
- Add `_handle_autonomy_enable`, `_handle_autonomy_disable`, `_handle_autonomy_status`
- Add to `SLASH_COMMANDS` dict

### `~/.hermes/plugins/colony/plugin.yaml`
- Update description to mention autonomy features

### Colony install script (`install.sh` or equivalent)
- Detect if Hermes is installed
- Prompt for autonomy enablement
- Call the plugin's enable method if user agrees

---

## 4. EDGE CASES

- **No Colony running:** Cron job gracefully fails, logs error, stays silent
- **No initiatives:** Agent outputs `[SILENT]`, no delivery
- **Quiet hours:** Agent respects Colony's quiet hours config; drafts but queues for later
- **Duplicate cron job:** `enable` checks for existing job by name, updates rather than duplicates
- **Contact not on WhatsApp:** Note it for the owner's review
- **Rate limits:** Respects platform rate limits via send_message tool

---

## 5. VERIFICATION CHECKLIST

- [ ] `colony_list_initiatives` tool returns initiatives
- [ ] `/colony autonomy enable` creates cron job in `jobs.json`
- [ ] `/colony autonomy status` shows correct state
- [ ] `/colony autonomy disable` pauses/removes job
- [ ] Cron job runs every 15m, stays silent when idle
- [ ] When initiatives exist, agent drafts messages or takes action
- [ ] Install script prompts for autonomy enablement
- [ ] Wizard path works end-to-end
