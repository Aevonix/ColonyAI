# Spec: ChannelRegistry — Per-Person Initiative Delivery Routing

**Author:** Aeva  
**Date:** 2026-05-16  
**Version:** 0.1.0  
**Status:** Draft — pending review

---

## 1. Problem Statement

All Colony initiatives currently route to the **home channel** (e.g., the WhatsApp group `GROUP_CHAT_ID@g.us`). This is wrong for personal check-ins and owner-directed initiatives, which should go to the owner's **DM channel** (e.g., the owner's personal WhatsApp).

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

---

## 3. Architecture

### 3.1 New Module: `colony_sidecar/delivery/channels.py`

```python
@dataclass
class Channel:
    """A resolved delivery channel."""
    platform: str        # "whatsapp", "telegram", "discord", ...
    chat_id: str         # platform-specific chat identifier
    label: str           # "dm", "home", "work", ...
    channel_type: str    # "dm" | "home" | "work" | "custom"


class ChannelRegistry:
    """Resolves per-person delivery channels from multiple sources.
    
    Resolution priority (highest first):
    1. Environment variables (COLONY_CHANNEL_*)
    2. JSON config file (~/.colony/data/channels.json)
    3. Contact handles (phone → whatsapp DM inference)
    4. Home channel fallback (current behavior)
    """
    
    def resolve(
        self,
        person_id: str,
        channel_type: str = "home",
    ) -> Optional[Channel]
```

### 3.2 Resolution Sources

**Source 1: Environment variables**

```bash
# Owner DM channel
COLONY_CHANNEL_DM_owner=whatsapp:+1555XXXXXXX
COLONY_CHANNEL_DM_owner=telegram:@username

# Other people's channels
COLONY_CHANNEL_DM_contact_a=whatsapp:+1555YYYYYYY

# Home channel (already exists via WHATSAPP_HOME_CHANNEL etc.)
```

**Source 2: JSON config file** (`~/.colony/data/channels.json`)

```json
{
  "contacts": {
    "owner": {
      "dm": {"platform": "whatsapp", "chat_id": "+1555XXXXXXX"},
      "home": {"platform": "whatsapp", "chat_id": "GROUP_CHAT_ID@g.us"}
    },
    "contact_a": {
      "dm": {"platform": "whatsapp", "chat_id": "+1555YYYYYYY"}
    }
  },
  "fallback": {
    "home": {"platform": "whatsapp", "chat_id": "GROUP_CHAT_ID@g.us"}
  }
}
```

**Source 3: Contact handles**

If a contact has a phone-number handle (`gateway="imessage"`, `address="++1555XXXXXXX"`), and the platform is WhatsApp, the DM channel can be inferred as `whatsapp:++1555XXXXXXX`.

> **Rationale for NOT using contact_handles for group chat IDs:**
> `contact_handles` stores *contact methods* (phone, email). WhatsApp group chat IDs are *conversation venues*, not contact methods. Mixing them in `contact_handles` would require a schema migration and would semantically pollute the table. A separate channel registry is cleaner.

**Source 4: Home channel fallback**

If no DM channel is resolved, fall back to the configured home channel (existing behavior).

---

## 4. Colony Changes

### 4.1 `colony_sidecar/delivery/channels.py` (new)

- `Channel` dataclass
- `ChannelRegistry` class with the 4 resolution sources
- `channel_registry_from_env()` helper
- File-backed persistence with atomic writes

### 4.2 `colony_sidecar/delivery/bridge.py` (modify)

**Inject `ChannelRegistry` into `ProactiveDeliveryBridge.__init__`:**

```python
def __init__(
    self,
    rate_limiter: Optional[DeliveryRateLimiter] = None,
    gateway_url: Optional[str] = None,
    gateway_api_key: Optional[str] = None,
    channel_registry: Optional[ChannelRegistry] = None,
) -> None:
    self._channel_registry = channel_registry or ChannelRegistry()
```

**Modify `push_initiative()` to populate `delivery_context`:**

```python
# Resolve channels for the target person
person_id = initiative.get("entity_id", "")
channel_hint = initiative.get("channel_hint", "home")

user_channel = self._channel_registry.resolve(person_id, "dm")
home_channel = self._channel_registry.resolve(person_id, "home")

delivery_context = {}
if user_channel:
    delivery_context["user_chat"] = f"{user_channel.platform}:{user_channel.chat_id}"
if home_channel:
    delivery_context["home_chat"] = f"{home_channel.platform}:{home_channel.chat_id}"

payload["delivery_context"] = delivery_context
```

### 4.3 `colony_sidecar/autonomy/checkin.py` (modify)

Add `channel_hint="dm"` to the initiative payload:

```python
payload = {
    "id": f"checkin-{datetime.now(timezone.utc).isoformat()}",
    "type": "proactive_message",
    "channel_hint": "dm",  # <-- NEW
    ...
}
```

### 4.4 `colony_sidecar/autonomy/synthesis.py` (modify)

Add `channel_hint="dm"` for personal goals and `channel_hint="home"` for system-level goals:

```python
# When creating a goal for a specific person
payload = {
    "type": "proactive_message",
    "channel_hint": "dm",  # <-- NEW: personal goal goes to DM
    ...
}
```

---

## 5. Hermes Config Changes

### 5.1 Update `~/.hermes/config.yaml` webhook prompt

The prompt already references `{payload.delivery_context.user_chat}`. Add a fallback rule:

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
      • For PERSONAL initiatives (channel_hint=dm or owner-directed), use `send_message` with target "{payload.delivery_context.user_chat}".
      • For SYSTEM initiatives (channel_hint=home or no hint), use `send_message` with target "{payload.delivery_context.home_chat}".
      • If the preferred channel is missing, fall back to the other channel.
      • If BOTH are missing, fall back to "whatsapp" (home channel).
    - Send AT MOST ONE message to the user per initiative. Make it concise — one or two sentences max.
    - Do NOT send multiple follow-up messages. Do NOT send "still working" updates.
    - If the initiative requires user input or is blocked, send ONE message asking what they want to do.
    - If you can complete the initiative autonomously, send ONE message summarizing what you did.
```

---

## 6. Configuration

### 6.1 Environment Variables

| Variable | Example | Description |
|----------|---------|-------------|
| `COLONY_CHANNEL_DM_{person_id}` | `whatsapp:+1555XXXXXXX` | DM channel for a specific person |
| `COLONY_CHANNEL_HOME_{person_id}` | `whatsapp:1203634...@g.us` | Home channel override per person |

> Note: `person_id` is case-insensitive and normalized (lowercased, spaces → underscores).

### 6.2 JSON Config File

Path: `~/.colony/data/channels.json`

```json
{
  "contacts": {
    "owner": {
      "dm": {"platform": "whatsapp", "chat_id": "+1555XXXXXXX"},
      "home": {"platform": "whatsapp", "chat_id": "GROUP_CHAT_ID@g.us"}
    }
  },
  "fallback": {
    "home": {"platform": "whatsapp", "chat_id": "GROUP_CHAT_ID@g.us"}
  }
}
```

### 6.3 Contact Handle Inference

If enabled (configurable), the registry can infer DM channels from contact handles:
- `gateway="imessage"`, `address="++1555XXXXXXX"` → infer `whatsapp:++1555XXXXXXX`
- `gateway="telegram"`, `address="@username"` → infer `telegram:@username`

Enable via: `COLONY_CHANNEL_INFER_FROM_HANDLES=true` (default: true)

---

## 7. API Changes

### 7.1 Initiative Payload Schema

Add optional fields:

```json
{
  "type": "initiative",
  "payload": { ... },
  "delivery_context": {
    "user_chat": "whatsapp:+1555XXXXXXX",
    "home_chat": "whatsapp:GROUP_CHAT_ID@g.us"
  }
}
```

### 7.2 Backwards Compatibility

- Old initiatives without `delivery_context` → Hermes prompt falls back to home channel
- New initiatives with `delivery_context` → prompt uses the resolved channels
- Missing `channel_hint` → defaults to `"home"`

---

## 8. Testing Strategy

### 8.1 Unit Tests

1. `ChannelRegistry.resolve()` — each resolution source independently
2. `ChannelRegistry.resolve()` — priority ordering (env → json → handles → fallback)
3. `ChannelRegistry` — case-insensitive person_id matching
4. `ProactiveDeliveryBridge.push_initiative()` — delivery_context populated correctly
5. `ProactiveDeliveryBridge.push_initiative()` — fallback when no DM configured

### 8.2 Integration Test

1. Configure `COLONY_CHANNEL_DM_marc=whatsapp:+1555XXXXXXX`
2. Trigger `OwnerCheckInTask`
3. Verify webhook payload contains `delivery_context.user_chat = "whatsapp:+1555XXXXXXX"`
4. Verify prompt substitution works in Hermes

---

## 9. Rollout Plan

1. **Phase 1: ChannelRegistry module** — build + unit tests
2. **Phase 2: Bridge integration** — inject registry, populate delivery_context
3. **Phase 3: Task updates** — add `channel_hint` to check-in and synthesis tasks
4. **Phase 4: Hermes prompt** — update `~/.hermes/config.yaml`
5. **Phase 5: E2E verification** — trigger initiative, confirm DM delivery

---

## 10. Open Questions

1. **Should we store channels in the graph (Neo4j) instead of a JSON file?**
   - JSON file is simpler for v1; graph integration could be v2
2. **Should channel data be exposed via the Colony API?**
   - Yes, add `GET /v1/channels/{person_id}` and `PUT /v1/channels/{person_id}` endpoints
3. **Should we support multiple DMs per person (e.g., WhatsApp + Telegram)?**
   - v1: single DM per person; v2: ranked preference list

---

## 11. References

- `colony_sidecar/delivery/bridge.py` — `ProactiveDeliveryBridge.push_initiative()`
- `colony_sidecar/autonomy/checkin.py` — `OwnerCheckInTask._emit_check_in()`
- `~/.hermes/config.yaml` — `colony-initiatives` webhook route prompt
- `colony_sidecar/contacts/models.py` — `ContactHandle` schema
