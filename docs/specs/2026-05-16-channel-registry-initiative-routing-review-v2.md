# Spec Review: ChannelRegistry v0.2.0

**Reviewer:** ColonyAI agent  
**Date:** 2026-05-16  
**Verdict:** **Minor issues found — fix before implementation**

---

## Summary

v0.2.0 successfully addresses the platform-agnostic concerns from the first review. No blockers remain. **5 issues** need fixing (1 medium, 4 low) to avoid implementation bugs or inconsistencies.

---

## Issues

### 1. 🟡 MEDIUM — Bare `ChannelRegistry()` init vs `load()` classmethod

**Location:** §4.3, line 187

**Current:**
```python
self._channel_registry = channel_registry or ChannelRegistry()
```

**Problem:** The architecture (§3.1) shows `ChannelRegistry.load()` as the classmethod for initialization, but the bridge fallback uses a bare `ChannelRegistry()` constructor. The class definition doesn't even show `__init__` — only `resolve()` and `load()`.

**Fix:**
```python
self._channel_registry = channel_registry or ChannelRegistry.load()
```

Or if the intent is that `__init__` exists but is minimal, define it explicitly in §3.1.

---

### 2. 🟢 LOW — `channel_registry_from_env()` mentioned but never defined

**Location:** §4.1

**Current:**
> - `channel_registry_from_env()` helper

**Problem:** The architecture shows `ChannelRegistry.load()` as the entrypoint. There's no need for a separate `from_env()` helper if `load()` handles all sources.

**Fix:** Remove the bullet or change to:
> - `ChannelRegistry.load()` — classmethod that loads from all 4 resolution sources

---

### 3. 🟢 LOW — "owner-directed" mentioned in prompt but no such field exists

**Location:** §5.1, line 266

**Current:**
> • For PERSONAL initiatives (channel_hint=dm or owner-directed), use `send_message` with target "..."

**Problem:** There is no "owner-directed" field or concept in the initiative schema. The only routing signal is `channel_hint`.

**Fix:**
> • For PERSONAL initiatives (channel_hint=dm), use `send_message` with target "..."

---

### 4. 🟢 LOW — Test description contradicts CLI-only section

**Location:** §8.1, test #6

**Current:**
> 6. `ChannelRegistry` — home channel guaranteed present via env fallback

**Problem:** Section 7.3 explicitly states that in CLI-only deployments (no `*_HOME_CHANNEL` env vars), `home_chat` may be absent. The test description contradicts this.

**Fix:**
> 6. `ChannelRegistry` — home channel resolved when env var present; absent in CLI-only mode

---

### 5. 🟢 LOW — Cross-platform example contradicts v1 limitation

**Location:** §7.1, lines 341–342 and §10, line 406

**Current example:**
```json
{
  "user_chat": "telegram:@username",
  "home_chat": "discord:#general"
}
```

**Current limitation:**
> v1 assumes DM and home use the same platform (e.g., both Telegram). Cross-platform (e.g., Telegram DM + Discord home) requires prompt updates and is deferred to v2.

**Problem:** The example shows cross-platform (Telegram + Discord) but v1 is documented as not supporting cross-platform. The prompt logic (§5.1) doesn't handle platform switching either — it just picks a `target` string and passes it to `send_message`.

**Fix:** Change the example to use the same platform:
```json
{
  "user_chat": "telegram:@username",
  "home_chat": "telegram:@groupname"
}
```

---

## Nitpicks (non-blocking)

| Location | Issue | Suggested Fix |
|----------|-------|---------------|
| §3.2, line 51 | "phone → whatsapp DM inference" | "phone → chat platform DM inference" |
| §3.2, line 133 | `1203634...@g.us` looks like a real ID prefix | Use `GROUP_ID@g.us` consistently |
| §4.5 | Code snippet only shows personal goal case | Add system-level snippet too |

---

## Positive Notes

1. **Platform diversity in examples** — Telegram, Discord, Signal all represented
2. **CLI-only handling** — section 7.3 correctly covers the no-platform case
3. **Source 4 generic pattern** — `{PLATFORM}_HOME_CHANNEL` scanning is clean and zero-config
4. **Backwards compatibility** — old initiatives without `delivery_context` still work
5. **No PII** — all examples use generic placeholders

---

## Conclusion

**Approve with fixes.** The 5 issues above are minor but would cause confusion or inconsistency during implementation. No re-review needed after fixes — proceed directly to implementation.
