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

**Root cause:** OpenClaw's plugin API doesn't have a direct "send message to channel" method. Plugins can only:
- Return results from hooks
- Register a `ProactiveDeliveryProvider` (if the host supports it)

**Solution options:**

### Option A: ProactiveDeliveryProvider (preferred)
OpenClaw has a `registerProactiveDeliveryProvider` API that lets plugins queue messages for the host to deliver. If available:

```typescript
api.registerProactiveDeliveryProvider({
  async getPendingDeliveries(channelId?: string): Promise<Delivery[]> {
    // Query sidecar's /delivery/pending
    return ctx.client.listPendingDeliveries({ gateway_id: channelId });
  },
  async markDelivered(deliveryId: string): Promise<void> {
    await ctx.client.markDeliverySent({ delivery_id: deliveryId });
  },
});
```

**Blocker:** Need to verify if OpenClaw SDK exposes this API.

### Option B: Event-driven delivery via gateway-specific hooks
If no proactive delivery API, we need to:
1. Listen for `proactive_message` events from sidecar
2. Call OpenClaw's `sendProactiveMessage(channelId, content)` if it exists
3. Or return a special result from `session_start` hook that queues delivery

**Blocker:** Need to check OpenClaw SDK for `sendProactiveMessage` or equivalent.

### Option C: Sidecar pushes directly to channels
Sidecar could have direct channel adapters (Telegram bot, Slack webhook, etc.) that push messages without going through the plugin. This bypasses OpenClaw's delivery infrastructure.

**Pros:** Works regardless of plugin API
**Cons:** Duplicates channel config, bypasses OpenClaw's rate limits and logging

**Plan:**

1. **Investigate (30 min):** Check OpenClaw SDK for:
   - `registerProactiveDeliveryProvider`
   - `sendProactiveMessage`
   - Any other proactive delivery mechanism
2. **Implement (1 hour):** Based on findings, wire the appropriate path
3. **Test (30 min):** Verify proactive messages flow through

**Time:** 2 hours (including investigation)

**Priority:** High — core feature

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

**Time:** 2.5 hours

**Priority:** Low — nice to have, not blocking current functionality

---

## Summary

| Item | Time | Priority | Dependencies |
|------|------|----------|--------------|
| 1. Missing client methods | 1.5h | Medium | None |
| 2. Proactive delivery | 2h | High | OpenClaw SDK investigation |
| 3. Server-side tools | 2.5h | Low | None |

**Recommended order:**

1. **Item 2 (Proactive delivery)** — highest impact, needs SDK investigation first
2. **Item 1 (Client methods)** — straightforward, no blockers
3. **Item 3 (Server-side tools)** — nice to have, lower priority

**Total estimated:** 6 hours

---

## Next Steps

1. Investigate OpenClaw SDK for proactive delivery APIs (Marc can help check docs)
2. Implement Item 2 based on findings
3. Add missing client methods (Item 1)
4. Consider server-side tools (Item 3) as future enhancement
