# Colony-Core Open Items Plan

## Overview

3 open items from the 2026-04-18 audit. Estimated total: 3-4 hours.

---

## Item 1: Missing Plugin Client Methods

**Problem:** Sidecar has 44 endpoints, plugin client only covers 16. Host code can't call 28 endpoints.

**Impact:** Medium — limits what the plugin can do, but most critical paths (memory, safety, reasoning, context, signals, turns) are already wired.

**Missing methods (28 endpoints → ~14 methods):**

| Category | Endpoints | Method to Add |
|----------|-----------|---------------|
| Autonomy | `/autonomy/status`, `/autonomy/start`, `/autonomy/stop` | `autonomyStatus()`, `autonomyStart()`, `autonomyStop()` |
| Briefings | `/briefings` | `listBriefings()` |
| Chain/Identity | `/identity/status`, `/identity/init`, `/chain/verify` | `identityStatus()`, `identityInit()`, `chainVerify()` |
| Cognition | `/cognition/cycle`, `/cognition/cpi` | `cognitionCycle()`, `getCPI()` (exists but returns unknown) |
| Contacts | `/contacts`, `/contacts/{id}`, `/contacts/{id}/style` | `listContacts()`, `getContact()`, `getContactStyle()` |
| Delivery | `/delivery/pending`, `/delivery/mark-sent` | `listPendingDeliveries()`, `markDeliverySent()` |
| Goals | `/goals`, `/goals/{id}`, PATCH | `listGoals()`, `getGoal()`, `updateGoal()` |
| Insights | `/insights`, `/insights/{id}/dismiss` | `listInsights()`, `dismissInsight()` |
| Learning | `/learning/correction`, `/learning/engagement` | `submitCorrection()`, `submitEngagement()` |
| Research | `/research`, `/research/start` | `listResearch()`, `startResearch()` |
| Secrets | `/secrets/list`, `/secrets/get`, `/secrets/set`, `/secrets/delete` | `listSecrets()`, `getSecret()`, `setSecret()`, `deleteSecret()` |
| Skills | `/skills/registry/{id}` | `getSkill()` |
| Synthesis | `/synthesis/discover` | `discoverConnections()` |
| World Model | `/world/entities`, `/world/entities/query` | `listEntities()`, `queryEntities()` |

**Plan:**

1. Add all methods to `src/sidecar-client.ts` with proper types from generated types
2. Add corresponding schemas to `src/types.ts` exports if not already exposed
3. Add tests for new methods (mock HTTP responses)
4. Regenerate types after any schema changes

**Time:** 1.5 hours

**Priority:** Medium — nice to have, not blocking

---

## Item 2: Events Lifecycle Service — Proactive Delivery

**Problem:** The events WebSocket receives events from the sidecar (anomalies, insights, proactive_message), but the plugin only logs them. It doesn't deliver proactive messages to channels.

**Impact:** High — Colony's proactive delivery feature doesn't work. The sidecar generates insights and initiatives, but they never reach the human.

**Root cause:** OpenClaw's plugin API doesn't have a direct "send message to channel" method.

### Investigation Results (2026-04-18)

**OpenClaw SDK analysis:**

Checked the following APIs:
- `OpenClawPluginApi` — no `sendProactiveMessage` or `registerProactiveDeliveryProvider`
- `PluginRuntime` — no direct message sending
- `PluginRuntimeChannel` — has `dispatchReplyFromConfig` but requires `FinalizedMsgContext` (reactive, not proactive)
- `PluginRuntime.subagent` — has `run({ sessionKey, message, deliver: true })` but spawns an agent turn

**Available workarounds:**

| Option | Pros | Cons |
|--------|------|------|
| A. `subagent.run({ deliver: true })` | Works now, uses existing API | Spawns full agent turn just to echo a message (inefficient) |
| B. Sidecar pushes directly | Works independently of plugin | Duplicates channel config, bypasses OpenClaw infrastructure |
| C. Request upstream feature | Most correct long-term | Requires waiting for OpenClaw release |
| D. Webhook + registerCommand | Works with current API | Hacky, requires webhook endpoint |

**Recommended approach:**

1. **Short-term:** Use `runtime.subagent.run({ sessionKey, message, deliver: true })` for proactive delivery
   - The "message" would be a minimal prompt like: `"Deliver this notification to the user: {content}"`
   - The agent responds immediately with the notification
   - Not ideal but functional

2. **Long-term:** Request OpenClaw add a `sendProactiveMessage(channelId, content)` API or `registerProactiveDeliveryProvider` interface

**Implementation:**

```typescript
// In plugin.ts, handle proactive_message events:
case "proactive_message": {
  const { channel_id, content, session_key } = event.payload;
  if (session_key) {
    await api.runtime.subagent.run({
      sessionKey: session_key,
      message: `Deliver this notification: ${content}`,
      deliver: true,
    });
  }
  break;
}
```

**Time:** 1 hour (subagent approach)

**Priority:** Medium — has a working workaround, not blocking

**Status:** ✅ Done — implemented in plugin.ts

---

## Item 3: ToolExecutor.get_definitions() Returns Empty

**Problem:** `ToolExecutor.get_definitions()` always returns `[]`. The ReasoningLoop never passes tool definitions to the LLM, so the model can't call Colony-native tools server-side.

**Impact:** Low — Colony currently relies on host-side tools (passed through the ReasoningLoop from OpenClaw). Server-side tools would be a new capability.

**What this enables:**
- Colony-native tools like `memory_search`, `relationship_score`, `goal_decompose`
- Server-side tool execution without host involvement
- Skill marketplace tools that run in the sidecar

**Solution:**

1. **Define Colony-native tools** in `sidecar/colony_sidecar/tools/`:
   ```python
   COLONY_TOOLS = [
       {
           "name": "colony_memory_search",
           "description": "Search Colony's memory graph for relevant context",
           "parameters": {
               "type": "object",
               "properties": {
                   "query": {"type": "string"},
                   "person_id": {"type": "string"},
               },
               "required": ["query"],
           },
       },
       {
           "name": "colony_get_relationship",
           "description": "Get relationship score and trust tier for a contact",
           "parameters": {
               "type": "object",
               "properties": {
                   "contact_id": {"type": "string"},
               },
               "required": ["contact_id"],
           },
       },
       # ... more tools
   ]
   ```

2. **Wire into ToolExecutor:**
   ```python
   def get_definitions(self, available_tools: list[str] | None = None) -> list[dict]:
       return COLONY_TOOLS
   ```

3. **Implement handlers** for each tool in the executor

4. **Wire into ReasoningLoop** — pass `tool_defs` to `model.complete()`

**Plan:**

1. **Define schema (30 min):** Create `tools/definitions.py` with 5-10 core Colony tools
2. **Wire definitions (15 min):** Update `ToolExecutor.get_definitions()`
3. **Implement handlers (1 hour):** Add async handlers for each tool
4. **Wire ReasoningLoop (15 min):** Pass definitions to LLM call
5. **Test (30 min):** Verify tool calls work end-to-end

**Time:** 1 hour

**Priority:** Low — nice to have, not blocking current functionality

**Status:** ✅ Done — 8 Colony-native tools defined and wired

---

## Summary

| Item | Time | Priority | Status |
|------|------|----------|--------|
| 1. Missing client methods | 1.5h | Medium | ✅ Done (42 methods, 95% coverage) |
| 2. Proactive delivery | 1h | Medium | ✅ Done (subagent workaround) |
| 3. Server-side tools | 2.5h | Low | ✅ Done (8 Colony tools) |

**Recommended order:**

1. ~~Item 2 (Proactive delivery)~~ — ✅ done
2. ~~Item 1 (Client methods)~~ — ✅ done
3. ~~Item 3 (Server-side tools)~~ — ✅ done

**All items complete.**

---

## Completion Summary

All 3 open items from the 2026-04-18 audit have been resolved:

1. ✅ **Missing client methods** — Added 27 methods, now 42 total (95% coverage)
2. ✅ **Proactive delivery** — Implemented subagent workaround
3. ✅ **Server-side tools** — Added 8 Colony-native tools

**Tests:** 186 passing (114 TS + 72 Python)

**Commits:** 6 pushed to main

**Remaining:** Only the 18 dependabot alerts (all dev dependencies, 0 production vulns)
