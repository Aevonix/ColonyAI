# Spec Review: ChannelRegistry — Per-Person Initiative Delivery Routing

**Reviewer:** Aeva (self-review)  
**Date:** 2026-05-16  
**Spec Version:** 0.1.0  
**Verdict:** **Needs revision** — 7 issues found, 3 design gaps, 1 blocker

---

## Executive Summary

The spec is directionally correct but has **critical gaps** in home-channel sourcing, registry initialization, and Hermes coupling. The core architecture (ChannelRegistry → delivery_context → prompt) is sound, but several details would cause bugs or require post-hoc fixes if implemented as written.

**Blocker:** The spec does not explain how Colony learns the home channel. Currently home channels live only in Hermes config (`WHATSAPP_HOME_CHANNEL`). Colony cannot populate `delivery_context.home_chat` without either (a) duplicating config or (b) reading Hermes files. Neither is addressed.

---

## Issues by Severity

### ⚠️ BLOCKER — Home Channel Source Undefined

**Location:** §4.2, §6.2, §7.1

**Problem:** The spec proposes populating `delivery_context.home_chat` but never explains where Colony gets the home channel value.

**Current state:**
- Home channel is configured in `~/.hermes/config.yaml` as `WHATSAPP_HOME_CHANNEL: GROUP_CHAT_ID`
- Colony has zero knowledge of home channels
- `ProactiveDeliveryBridge` does not read Hermes config

**If implemented as written:** `home_chat` would be `None` for all initiatives unless the user manually creates `~/.colony/data/channels.json` with a fallback section. This is a silent failure — initiatives would lose their home channel fallback.

**Fix options:**

| Option | Approach | Trade-off |
|--------|----------|-----------|
| A | Colony reads `~/.hermes/config.yaml` at startup | Tight coupling; Hermes config format may change |
| B | Colony home channel configured via env var (`COLONY_HOME_CHANNEL_whatsapp=...`) | Clean separation; requires user to set one more var |
| C | `ProactiveDeliveryBridge` falls back to existing env vars (`WHATSAPP_HOME_CHANNEL` etc.) | Bridge already runs in same env; zero new config |

**Recommendation: Option C + B.**
- Phase 1: Bridge checks existing `WHATSAPP_HOME_CHANNEL` / `TELEGRAM_HOME_CHANNEL` env vars (already in the process environment) as a fallback source in ChannelRegistry
- Phase 2 (optional): Support `COLONY_CHANNEL_HOME_*` env vars for explicit override

This avoids coupling and requires zero new config for existing setups.

---

### 🔴 HIGH — Registry Initialization Missing

**Location:** §4.2, §9

**Problem:** The spec shows `ChannelRegistry` injected into `ProactiveDeliveryBridge` but never shows how the registry singleton is created in `server.py` lifespan.

**Gap:**
- Where does the JSON file get loaded?
- When does env-var scanning happen?
- Is the registry recreated on every initiative push or cached?

**Fix:** Add explicit server.py integration:

```python
# server.py lifespan
channel_registry = ChannelRegistry.load(
    json_path=f"{state_dir}/data/channels.json",
    env_prefix="COLONY_CHANNEL_",
)
app.state.channel_registry = channel_registry
# ... later passed to ProactiveDeliveryBridge(..., channel_registry=channel_registry)
```

Also: `ChannelRegistry.load()` should be idempotent and log which sources were loaded (env count, file path, handle inference enabled/disabled).

---

### 🔴 HIGH — Prompt Fallback Wording Is Wrong

**Location:** §5.1, line 206

**Current spec text:**
> • If BOTH are missing, fall back to "whatsapp" (home channel).

**Problem:** `"whatsapp"` is not a valid `send_message` target. Targets must be `platform:chat_id` (e.g., `whatsapp:GROUP_CHAT_ID@g.us`) or platform-specific formats.

**Fix:**
> • If BOTH are missing, send a log entry only — do not message the user. The initiative data is preserved in logs for manual review.

Actually, if `home_chat` is always populated (via the env-var fallback in Option C above), "both missing" should never happen. The prompt should state:
> • `home_chat` is guaranteed to be present. `user_chat` may be absent if no DM is configured.

This simplifies the prompt and removes the broken fallback.

---

### 🔴 HIGH — Contact Handle Inference Is Under-Specified

**Location:** §3.2 Source 3, §6.3

**Problems:**
1. Gateway-to-platform mapping is hardcoded but not documented:
   - `gateway="imessage"` → what platform? WhatsApp? iMessage? The spec says WhatsApp but that's your setup, not generic.
   - `gateway="sms"` → same ambiguity
   - `gateway="telegram"` → Telegram (clear)
   - `gateway="email"` → Email (clear, but email is not a chat platform)

2. Phone number normalization: handles are stored in various formats (`+1 555 123 4567`, `15551234567`, etc.). The spec doesn't reference `normalize_handle()` from `contacts/models.py`.

3. The double-plus bug: scrubbed spec shows `++1555XXXXXXX` (line 99, 247) instead of `+1555XXXXXXX`.

**Fix:**
Add an explicit mapping config:

```python
# ChannelRegistry config
handle_gateway_map = {
    "imessage": "whatsapp",   # or "imessage" if using BlueBubbles
    "sms": "whatsapp",
    "telegram": "telegram",
    "signal": "signal",
    # "email" excluded — not a chat platform
}
```

Override via env: `COLONY_CHANNEL_GATEWAY_MAP='{"imessage":"imessage"}'`

Also reference `normalize_handle()` for phone-number normalization.

---

### 🟡 MEDIUM — `label` vs `channel_type` Redundancy

**Location:** §3.1

```python
@dataclass
class Channel:
    platform: str
    chat_id: str
    label: str           # "dm", "home", "work", ...
    channel_type: str    # "dm" | "home" | "work" | "custom"
```

**Problem:** `label` and `channel_type` serve the same purpose.

**Fix:** Remove `label`. `channel_type` is the canonical field. If a display label is needed, derive it from `channel_type`.

---

### 🟡 MEDIUM — "File-backed persistence with atomic writes" Is Misleading

**Location:** §4.1

**Problem:** The spec says ChannelRegistry has "file-backed persistence with atomic writes" but never defines write methods. The registry is **read-only config** — it's loaded from env + JSON at startup.

**Fix:** Change to:
> • Config-backed resolution — reads from env vars + JSON file at startup; optional `reload()` method to re-read without restart.

If writes are desired (API endpoints), that should be a v2 feature.

---

### 🟡 MEDIUM — Integration Test Still Uses Real Name

**Location:** §8.2, line 291

**Current:**
> 1. Configure `COLONY_CHANNEL_DM_owner=whatsapp:+1555XXXXXXX`

**Fix:**
> 1. Configure `COLONY_CHANNEL_DM_owner=whatsapp:+1555XXXXXXX`

---

### 🟡 MEDIUM — Missing: Platform-Specific Chat ID Formats

**Location:** §7.1

**Problem:** The spec shows `whatsapp:+1555XXXXXXX` and `whatsapp:GROUP_CHAT_ID@g.us` but doesn't document that chat ID formats vary by platform:
- WhatsApp DM: `+1555XXXXXXX` or `LID@lid`
- WhatsApp group: `GROUP_ID@g.us`
- Telegram: `@username` or numeric chat ID
- Discord: `#channel-name` or numeric ID
- iMessage (BlueBubbles): `chat_guid:...`

**Fix:** Add a platform format reference table to the spec. The registry itself doesn't validate formats (that's the platform adapter's job), but the config examples should show the correct patterns.

---

### 🟢 LOW — `COLONY_CHANNEL_HOME_{person_id}` Is Likely Unused

**Location:** §6.1

**Problem:** Per-person home channel override is an edge case. Most deployments have one home channel for everyone.

**Fix:** Keep it (it's harmless) but mark as "advanced / optional" in docs. The primary home channel should come from the global fallback or env var.

---

### 🟢 LOW — Missing API Endpoints Definition

**Location:** §10

**Problem:** Open Question 2 asks "Should channel data be exposed via API?" but doesn't define what those endpoints would look like.

**Fix:** For v1, explicitly scope API endpoints OUT. Add a note:
> **v1 scope:** No API endpoints. Channel config is file-based only. API endpoints (`GET /v1/channels/{person_id}`, `PUT /v1/channels/{person_id}`) are deferred to v2.

---

### 🟢 LOW — Path Should Use `COLONY_STATE_DIR`

**Location:** §6.2

**Current:** `~/.colony/data/channels.json`

**Fix:** `{COLONY_STATE_DIR}/data/channels.json` with a note that `COLONY_STATE_DIR` defaults to `~/.colony`.

---

## Design Gaps

### Gap 1: Multi-Platform DMs

**What if the owner uses WhatsApp for home channel but Telegram for DMs?**

The spec assumes DM and home use the same platform. The `Channel` dataclass has `platform` per channel, so technically it supports cross-platform. But the bridge's `delivery_context` would be:
```json
{
  "user_chat": "telegram:@username",
  "home_chat": "whatsapp:GROUP_CHAT_ID@g.us"
}
```

The Hermes prompt would need to handle different platforms in the same payload. The current prompt assumes both are the same platform.

**Verdict:** Acceptable for v1. Add a note that cross-platform routing requires prompt updates.

### Gap 2: What About System-Wide Initiatives With No Person?

**Example:** "Disk space low" or "Colony sidecar down" alert.

The spec says `person_id = initiative.get("entity_id", "")`. If empty, DM resolution returns `None`, and we fall back to home channel. This is correct but implicit.

**Fix:** Add explicit logic in §4.2:
```python
if not person_id:
    # System initiative — no DM, always home
    user_channel = None
    home_channel = self._channel_registry.resolve("__system__", "home") or self._home_channel_fallback
```

### Gap 3: Hermes Config Change Scope

The spec says update `~/.hermes/config.yaml` but doesn't show the FULL prompt — only the delta. The reviewer (the owner) needs to see the complete prompt to evaluate it.

**Fix:** Add an appendix with the full proposed prompt.

---

## Recommendations

### Immediate (before implementation)
1. **Fix the home channel source** (§4.2, §6) — define how Colony learns the home channel without coupling to Hermes
2. **Add server.py integration** (§4) — show registry initialization
3. **Fix prompt fallback wording** (§5.1) — remove `"whatsapp"` string fallback
4. **Define gateway-to-platform mapping** (§3.2, §6.3) — make inference generic and configurable
5. **Remove `label` field** (§3.1) — redundant with `channel_type`
6. **Fix integration test PII** (§8.2) — `owner` → `owner` (already generic)
7. **Fix `++` double-plus bug** (§6.3) — `++1555` → `+1555`

### Deferred to v2
- API endpoints for channel CRUD
- Multi-DM ranked preferences per person
- Neo4j-backed channel storage
- Cross-platform prompt handling

---

## Revised Architecture Summary

```
┌─────────────────────────────────────────────────────────────┐
│  ChannelRegistry (singleton, loaded at server startup)              │
│                                                                      │
│  Resolution priority:                                                 │
│  1. Env vars: COLONY_CHANNEL_DM_owner=whatsapp:+1555XXXXXXX          │
│  2. JSON file: {COLONY_STATE_DIR}/data/channels.json                 │
│  3. Contact handles: gateway="imessage" → platform="whatsapp"         │
│  4. Home fallback: WHATSAPP_HOME_CHANNEL env var                     │
│                                                                      │
│  resolve(person_id, channel_type) → Channel(platform, chat_id, type) │
└─────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────┐
│  ProactiveDeliveryBridge.push_initiative()                            │
│                                                                      │
│  user_channel = registry.resolve(entity_id, "dm")                    │
│  home_channel = registry.resolve(entity_id, "home")                  │
│                                                                      │
│  payload["delivery_context"] = {                                      │
│      "user_chat": f"{user.platform}:{user.chat_id}"  if user else None,│
│      "home_chat": f"{home.platform}:{home.chat_id}"  # always present  │
│  }                                                                    │
└─────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────┐
│  Hermes Webhook Prompt                                               │
│                                                                      │
│  • PERSONAL (channel_hint=dm) → send_message target=user_chat       │
│  • SYSTEM  (channel_hint=home) → send_message target=home_chat      │
│  • user_chat missing → fall back to home_chat                        │
│  • home_chat always present (guaranteed)                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Conclusion

**Do not implement v0.1.0 as written.** The home channel sourcing gap is a blocker that would break all initiative delivery. The other issues are fixable but would create tech debt.

**Recommended path:**
1. Revise spec to v0.2.0 addressing all BLOCKER and HIGH issues
2. Re-review with the owner
3. Then implement
