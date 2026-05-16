# Spec: ChannelRegistry — Per-Person Initiative Delivery Routing

**Author:** Aeva  
**Date:** 2026-05-16  
**Version:** 0.2.0  
**Status:** Draft — pending review

---

## 1. Problem Statement

All Colony initiatives currently route to the **home channel** (e.g., a Telegram group, a Discord server, or a WhatsApp group). This is wrong for personal check-ins and owner-directed initiatives, which should go to the owner's **DM channel** (e.g., the owner's personal Telegram, Discord DM, or WhatsApp chat).

The Hermes webhook prompt already references `{payload.delivery_context.user_chat}` for DM routing, but Colony's `push_initiative()` **never populates** the `delivery_context` field. This is a data gap, not an architectural gap.

### Pain
- Owner check-ins appear in the home channel → noisy for other participants
- Personal goal reminders appear in the home channel → privacy concern
- No generic mechanism to route per-person initiatives to their preferred channel

---

## 2. Goals

1. **Generic per-person channel routing** — any initiative can declare a `channel_hint` and the system resolves the right `(platform, chat_id)`
2. **Zero Hermes source changes** — the webhook prompt already supports `delivery_context.user_chat`; we just populate it
3. **Zero database migrations** — use JSON config file + env vars; avoid schema changes to `contact_handles`
4. **Backwards compatible** — if no DM configured, fall back to home channel (current behavior)
5. **Platform-agnostic** — works with any Hermes-supported platform (WhatsApp, Telegram, Discord, iMessage, Signal, CLI-only)
---

## 3. Architecture

### 3.1 New Module: `colony_sidecar/delivery/channels.py`

```python
@dataclass
class Channel:
    """A resolved delivery channel."""
    platform: str        # "whatsapp", "telegram", "discord", ...
    chat_id: str         # platform-specific chat identifier
    channel_type: str    # "dm" | "home" | "work" | "custom"


class ChannelRegistry:
    """Resolves per-person delivery channels from multiple sources.
    
    Resolution priority (highest first):
    1. Environment variables (COLONY_CHANNEL_*)
    2. JSON config file ({COLONY_STATE_DIR}/data/channels.json)
    3. Contact handles (phone → chat platform DM inference, configurable mapping)
    4. Home channel fallback (WHATSAPP_HOME_CHANNEL, TELEGRAM_HOME_CHANNEL, etc.)
    """
    
    def resolve(
        self,
        person_id: str,
        channel_type: str = "home",
    ) -> Optional[Channel]
```

### 3.2 Resolution Sources

**Source 1: Environment variables (highest priority)**

```bash
# Owner DM channel (any platform)
COLONY_CHANNEL_DM_owner=telegram:@username
COLONY_CHANNEL_DM_owner=discord:USER_ID
COLONY_CHANNEL_DM_owner=whatsapp:+1555XXXXXXX

# Other people's channels
COLONY_CHANNEL_DM_contact_a=signal:+1555YYYYYYY

# Global home channel override (falls back to existing PLATFORM_HOME_CHANNEL env vars)
COLONY_CHANNEL_HOME=telegram:@groupname
```

> `person_id` is case-insensitive and normalized (lowercased, spaces → underscores).

**Source 2: JSON config file** (`{COLONY_STATE_DIR}/data/channels.json`)

```json
{
  "contacts": {
    "owner": {
      "dm": {"platform": "telegram", "chat_id": "@username"},
      "home": {"platform": "discord", "chat_id": "#general"}
    },
    "contact_a": {
      "dm": {"platform": "signal", "chat_id": "+1555YYYYYYY"}
    }
  },
  "fallback": {
    "home": {"platform": "discord", "chat_id": "#general"}
  }
}
```

> `COLONY_STATE_DIR` defaults to `~/.colony`.

**Source 3: Contact handles (configurable inference)**

If enabled (`COLONY_CHANNEL_INFER_FROM_HANDLES=true`, default: true), the registry inspects contact handles and infers DM channels via a configurable gateway-to-platform mapping:

```python
handle_gateway_map = {
    "imessage": "whatsapp",   # override via COLONY_CHANNEL_GATEWAY_MAP
    "sms": "whatsapp",
    "telegram": "telegram",
    "signal": "signal",
    # "email" excluded — not a chat platform
}
```

| Handle Gateway | Inferred Platform | Example Handle | Inferred Channel |
|----------------|-------------------|----------------|------------------|
| `imessage` | `whatsapp` | `+15551234567` | `whatsapp:+15551234567` |
| `sms` | `whatsapp` | `+15551234567` | `whatsapp:+15551234567` |
| `telegram` | `telegram` | `@username` | `telegram:@username` |

Phone numbers are normalized via `normalize_handle()` from `contacts/models.py` before inference.

> **Rationale for NOT using contact_handles for group chat IDs:**
> `contact_handles` stores *contact methods* (phone, email). WhatsApp group chat IDs are *conversation venues*, not contact methods. Mixing them in `contact_handles` would require a schema migration and would semantically pollute the table. A separate channel registry is cleaner.

**Source 4: Home channel fallback (lowest priority)**

If no DM channel is resolved, the registry falls back to the global home channel configured via existing environment variables:

| Platform | Env Var | Example Value |
|----------|---------|---------------|
| WhatsApp | `WHATSAPP_HOME_CHANNEL` | `GROUP_ID@g.us` or `+1555...` |
| Telegram | `TELEGRAM_HOME_CHANNEL` | `@groupname` or numeric ID |
| Discord | `DISCORD_HOME_CHANNEL` | `#channel-name` or numeric ID |

This source scans the process environment for any variable matching the pattern `{PLATFORM}_HOME_CHANNEL` (e.g., `WHATSAPP_HOME_CHANNEL`, `TELEGRAM_HOME_CHANNEL`, `DISCORD_HOME_CHANNEL`, `SIGNAL_HOME_CHANNEL`).

Mapping: strip `_HOME_CHANNEL` suffix, lowercase the platform name:
- `WHATSAPP_HOME_CHANNEL` → platform `"whatsapp"`
- `TELEGRAM_HOME_CHANNEL` → platform `"telegram"`
- `DISCORD_HOME_CHANNEL` → platform `"discord"`

If multiple home channels are configured, the first one found (alphabetical by env var name) is used as the default. A specific platform can be forced via the JSON fallback section.

This requires zero new configuration for existing deployments — the env vars are already present if the user has configured a home channel for Hermes.

---

## 4. Colony Changes

### 4.1 `colony_sidecar/delivery/channels.py` (new)

- `Channel` dataclass
- `ChannelRegistry` class with the 4 resolution sources
- `ChannelRegistry.load()` classmethod — idempotent, logs which sources were loaded
- Optional `reload()` method to re-read config without restart

### 4.2 Server Startup Integration

The registry is loaded once at server startup and cached as a singleton:

```python
# server.py lifespan
channel_registry = ChannelRegistry.load(
    json_path=f"{state_dir}/data/channels.json",
    env_prefix="COLONY_CHANNEL_",
)
app.state.channel_registry = channel_registry
# ... later passed to ProactiveDeliveryBridge(..., channel_registry=channel_registry)
```

`ChannelRegistry.load()` is idempotent and logs which sources were loaded (env count, file path, handle inference enabled/disabled).

### 4.3 `colony_sidecar/delivery/bridge.py` (modify)

**Inject `ChannelRegistry` into `ProactiveDeliveryBridge.__init__`:**

```python
def __init__(
    self,
    rate_limiter: Optional[DeliveryRateLimiter] = None,
    gateway_url: Optional[str] = None,
    gateway_api_key: Optional[str] = None,
    channel_registry: Optional[ChannelRegistry] = None,
) -> None:
    self._channel_registry = channel_registry or ChannelRegistry.load()
```

**Modify `push_initiative()` to populate `delivery_context`:**

```python
# Resolve channels for the target person
person_id = initiative.get("entity_id", "")
channel_hint = initiative.get("channel_hint", "home")

if not person_id:
    # System initiative — no DM, always home
    user_channel = None
    home_channel = self._channel_registry.resolve("__system__", "home")
else:
    user_channel = self._channel_registry.resolve(person_id, "dm")
    home_channel = self._channel_registry.resolve(person_id, "home")

delivery_context = {}
if user_channel:
    delivery_context["user_chat"] = f"{user_channel.platform}:{user_channel.chat_id}"
if home_channel:
    delivery_context["home_chat"] = f"{home_channel.platform}:{home_channel.chat_id}"

payload["delivery_context"] = delivery_context
```

> `user_chat` may be absent if no DM is configured. `home_chat` may be absent in CLI-only deployments with no chat platform configured. The prompt handles both cases.

### 4.4 `colony_sidecar/autonomy/checkin.py` (modify)

Add `channel_hint="dm"` to the initiative payload:

```python
payload = {
    "id": f"checkin-{datetime.now(timezone.utc).isoformat()}",
    "type": "proactive_message",
    "channel_hint": "dm",  # <-- NEW
    ...
}
```

### 4.5 `colony_sidecar/autonomy/synthesis.py` (modify)

Add `channel_hint="dm"` for personal goals and `channel_hint="home"` for system-level goals:

```python
# Personal goal → DM
payload = {
    "type": "proactive_message",
    "channel_hint": "dm",
    "entity_id": goal.person_id,
    ...
}

# System-wide goal → home
payload = {
    "type": "proactive_message",
    "channel_hint": "home",
    # no entity_id — system initiative
    ...
}
```

---

## 5. Hermes Config Changes

### 5.1 Update `~/.hermes/config.yaml` webhook prompt

The prompt already references `{payload.delivery_context.user_chat}`. Add explicit channel selection rules:

```yaml
colony-initiatives:
  deliver: log
  prompt: |
    Colony initiative received:
    Type: {payload.initiative_type}
    Title: {payload.title}
    Description: {payload.description}
    Priority: {payload.priority}
    Suggested actions: {payload.context.suggested_actions}

    You are {payload.agent_name}, an autonomous agent. Act on this initiative immediately.

    DELIVERY RULES:
    - Your FULL response (detailed reasoning, tool outputs, findings) goes to LOGS only.
    - If you need to notify the user of the outcome:
      • For PERSONAL initiatives (channel_hint=dm), use `send_message` with target "{payload.delivery_context.user_chat}".
      • For SYSTEM initiatives (channel_hint=home or no hint), use `send_message` with target "{payload.delivery_context.home_chat}".
      • If the preferred channel is missing, fall back to the other channel.
      • If BOTH channels are missing (CLI-only deployment), log the result and do not attempt to send a message.
    - Send AT MOST ONE message to the user per initiative. Make it concise — one or two sentences max.
    - Do NOT send multiple follow-up messages. Do NOT send "still working" updates.
    - If the initiative requires user input or is blocked, send ONE message asking what they want to do.
    - If you can complete the initiative autonomously, send ONE message summarizing what you did.
```

> Both `user_chat` and `home_chat` are optional. The prompt always prefers the channel matching the initiative's `channel_hint`, and falls back to the other when missing. In CLI-only deployments with no chat platform, both may be absent — the agent logs the result without messaging.

---

## 6. Configuration

### 6.1 Environment Variables

| Variable | Example | Description |
|----------|---------|-------------|
| `COLONY_CHANNEL_DM_{person_id}` | `telegram:@username` | DM channel for a specific person |
| `COLONY_CHANNEL_HOME` | `discord:#general` | Global home channel override (advanced/optional) |
| `COLONY_CHANNEL_GATEWAY_MAP` | `{"imessage":"signal"}` | JSON override for handle-to-platform inference mapping |
| `COLONY_CHANNEL_INFER_FROM_HANDLES` | `true` | Enable contact handle inference (default: true) |
| `WHATSAPP_HOME_CHANNEL` | `GROUP_ID@g.us` | WhatsApp home (existing, read by registry) |
| `TELEGRAM_HOME_CHANNEL` | `@groupname` | Telegram home (existing, read by registry) |
| `DISCORD_HOME_CHANNEL` | `#general` | Discord home (existing, read by registry) |

> `person_id` is case-insensitive and normalized (lowercased, spaces → underscores).

### 6.2 JSON Config File

Path: `{COLONY_STATE_DIR}/data/channels.json`

```json
{
  "contacts": {
    "owner": {
      "dm": {"platform": "telegram", "chat_id": "@username"},
      "home": {"platform": "discord", "chat_id": "#general"}
    },
    "contact_a": {
      "dm": {"platform": "signal", "chat_id": "+1555YYYYYYY"}
    }
  },
  "fallback": {
    "home": {"platform": "discord", "chat_id": "#general"}
  }
}
```

### 6.3 Platform-Specific Chat ID Formats

The registry stores chat IDs as opaque strings; validation is the platform adapter's responsibility. Config authors should use the correct format for each platform:

| Platform | DM Format | Group Format |
|----------|-----------|--------------|
| WhatsApp | `+1555XXXXXXX` or `LID@lid` | `GROUP_ID@g.us` |
| Telegram | `@username` or numeric chat ID | `-123456789` or `@groupname` |
| Discord | `#channel-name` or numeric ID | numeric ID |
| iMessage (BlueBubbles) | `chat_guid:...` | `chat_guid:...` |

---

## 7. API Changes

### 7.1 Initiative Payload Schema

Add optional fields:

```json
{
  "type": "initiative",
  "payload": { ... },
  "delivery_context": {
    "user_chat": "telegram:@username",
    "home_chat": "telegram:@groupname"
  }
}
```

### 7.2 Backwards Compatibility

- Old initiatives without `delivery_context` → Hermes prompt falls back to home channel
- New initiatives with `delivery_context` → prompt uses resolved channels
- Missing `channel_hint` → defaults to `"home"`
- Missing both channels → agent logs only, no message (CLI-only deployment)

### 7.3 CLI-Only Deployments

If no chat platform is configured (no `*_HOME_CHANNEL` env vars), both `user_chat` and `home_chat` will be absent from `delivery_context`. The prompt instructs the agent to log the result without attempting to send a message. The initiative is still processed and logged — the user reviews logs via CLI or file.

---

## 8. Testing Strategy

### 8.1 Unit Tests

1. `ChannelRegistry.resolve()` — each resolution source independently
2. `ChannelRegistry.resolve()` — priority ordering (env → json → handles → fallback)
3. `ChannelRegistry` — case-insensitive person_id matching
4. `ProactiveDeliveryBridge.push_initiative()` — delivery_context populated correctly
5. `ProactiveDeliveryBridge.push_initiative()` — fallback when no DM configured
6. `ChannelRegistry` — home channel resolved when env var present; absent in CLI-only mode

### 8.2 Integration Test

1. Configure `TELEGRAM_HOME_CHANNEL=@groupname` (existing env)
2. Configure `COLONY_CHANNEL_DM_owner=telegram:@username`
3. Trigger `OwnerCheckInTask`
4. Verify webhook payload contains:
   - `delivery_context.user_chat = "telegram:@username"`
   - `delivery_context.home_chat = "telegram:@groupname"`
   - `channel_hint = "dm"`
5. Verify prompt substitution works in Hermes
6. Verify `delivery_context.home_chat` is present even with no explicit home config (via env fallback)
7. Test CLI-only mode: unset all `*_HOME_CHANNEL` vars, verify initiative is processed but no delivery_context is sent

---

## 9. Rollout Plan

1. **Phase 1: ChannelRegistry module** — build + unit tests
2. **Phase 2: Server integration** — registry singleton, lifespan wiring
3. **Phase 3: Bridge integration** — inject registry, populate delivery_context
4. **Phase 4: Task updates** — add `channel_hint` to check-in and synthesis tasks
5. **Phase 5: Hermes prompt** — update `~/.hermes/config.yaml`
6. **Phase 6: E2E verification** — trigger initiative, confirm DM delivery

---

## 10. Open Questions (Deferred to v2)

1. **Should we store channels in the graph (Neo4j) instead of a JSON file?**
   - JSON file is simpler for v1; graph integration could be v2
2. **Should channel data be exposed via the Colony API?**
   - **v1 scope:** No API endpoints. Channel config is file-based only. API endpoints (`GET /v1/channels/{person_id}`, `PUT /v1/channels/{person_id}`) are deferred to v2.
3. **Should we support multiple DMs per person (e.g., WhatsApp + Telegram)?**
   - v1: single DM per person; v2: ranked preference list
4. **Cross-platform routing**
   - v1 assumes DM and home use the same platform (e.g., both Telegram). Cross-platform (e.g., Telegram DM + Discord home) requires prompt updates and is deferred to v2.

---

## 11. References

- `colony_sidecar/delivery/bridge.py` — `ProactiveDeliveryBridge.push_initiative()`
- `colony_sidecar/autonomy/checkin.py` — `OwnerCheckInTask._emit_check_in()`
- `~/.hermes/config.yaml` — `colony-initiatives` webhook route prompt
- `colony_sidecar/contacts/models.py` — `ContactHandle` schema, `normalize_handle()`
