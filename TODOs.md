# ColonyAI Open TODOs and Action Items

*Compiled: 2026-05-18*
*Repo: ~/colony-work (github.com/Aevonix/ColonyAI)*

---

## 1. CODE TODOs (In-Source Comments)

### High Priority

| Location | Line | Issue |
|----------|------|-------|
| `sidecar/colony_sidecar/agent/client.py` | 187 | `current_assignments: 0` — not tracking actual assignments |
| `sidecar/colony_sidecar/api/routers/host.py` | 4633 | Node certificate uses simplified signing (not real PKI) |
| `sidecar/colony_sidecar/api/routers/host.py` | 4640 | Node signature is `f"sig-{uuid}"` placeholder, not cryptographically valid |
| `sidecar/colony_sidecar/cli.py` | 710 | Node public key is random UUID, not a real keypair |

### Medium Priority

| Location | Line | Issue |
|----------|------|-------|
| `src/plugin.ts` | 1165 | Reasoning capability probe returns empty on any error; transient blips flip reasoning off |
| `src/plugin.ts` | 1940 | `outgoing_message` and `channel_id` extraction incomplete (Phase 7+) |
| `sidecar/colony_sidecar/goals/inference.py` | 195 | LLM interpretation pass extension point never wired in production |

---

## 2. DEFERRED ITEMS (Consciously Postponed)

From `docs/deferred-items.md` (last updated 2026-04-23):

### Security / Auth
- **MCP Provenance Not Stored in Schemas** — `COLONY_MCP_SOURCE` dropped by Pydantic on schemas without `metadata` field
- **MCP Session ID Hardcoded to "mcp"** — All MCP calls share one session, ruining analytics
- **Bearer Auth Not Systematically Tested** — Only 2 of 117 endpoints checked for auth enforcement
- **No Adversarial Tests for Skill Security Scanner** — Only 4 positive cases; no obfuscation/socket/lambda escape tests

### Performance / Architecture
- **Proactive Delivery Spawns Full Subagent Turn** — Burns tokens just to echo a notification (since v0.5.x)
- **Prefetch Cache Race in Hermes Provider** — Async task clears cache; overlapping call sees empty and duplicates HTTP request
- **ThreadPoolExecutor Per Call in Aggregators** — New executor per invocation under burst load
- **Naive datetime.now() Mixed with tz-aware** — 46 naive vs 205 aware; riskiest in initiative engine

### Testing / Quality
- **World-Model Schema Migrations Untested** — No test validates fresh DB + migrations == incrementally migrated DB
- **Compression Edge Cases Untested** — No tests for budget underflow, title longer than budget, oversized query
- **E2E Tests Use Hardcoded time.sleep()** — Flaky under CI load
- **TS Tests Mock Entire OpenClaw SDK** — Real integration breakage won't be caught

### Documentation / Cosmetic
- **README Miscounts** — Claims 36 subsystems (actual 38), "34 checks" (actual 38), "57+" endpoints (actual 117 decorators)
- **Stub*Aggregator Naming** — `Stub*Aggregator` looks unfinished but is intentional no-op fallback
- **501 "Not Wired" Endpoint Boilerplate** — ~50 endpoints return `HTTPException(501)` individually; could use decorator

### iMessage
- **iMessage Truncation UTF-8 Edge Case** — Char-based slicing may overshoot bytes on emoji-dense bodies

---

## 3. SPEC-LEVEL OPEN ITEMS

The loose design and analysis docs under `specs/` and `docs/specs/` have been
removed (implemented or superseded). The decisions that still stand on their own:

- **Initiative → channel routing** — the original "modify 4 Hermes core files"
  approach (`webhook.py`, `run.py`, `hermes_state.py`, `tools/colony_proactive.py`)
  is dead. Any routing work must live within existing Hermes extension points or a
  Colony-side-only contract.
- **Channel registry** — confirm whether registry-driven routing landed or is
  still pending.

---

## 4. HERMES INTEGRATION NOTES

- **`plugins/hermes-memory/`** — Async rewrite partially done; prefetch race narrowed but not fully fixed
- **`plugins/hermes-plugin/SPEC.md`** — Colony plugin spec for Hermes; should be reviewed for drift vs. actual implementation
- **No commits should be made to `~/.hermes/hermes-agent/`** — That repo is NousResearch upstream, not Aevonix. One unpushed commit (`9c396ef18`) exists on that repo authored by this machine's git config. Recommend `git reset --soft HEAD~1` to remove it.

---

## 5. SUMMARY BY PRIORITY

### Blockers (Do Before Next Release)
1. Initiative → channel routing — choose a Hermes-extension-point or Colony-side-only contract (the core-file-modification approach is dead)
2. Node certificate/signing is fake — security risk if multi-agent networking goes live
3. `current_assignments: 0` hardcoded — affects agent load balancing accuracy

### High (Next Sprint)
4. Proactive delivery spawns full agent turn — token waste since v0.5.x
5. MCP provenance dropped by Pydantic — per-harness attribution broken
6. MCP session ID hardcoded — analytics meaningless for MCP traffic
7. datetime naive/aware mix — initiative engine will TypeError eventually

### Medium (Backlog)
8. Reasoning capability probe fragile on network blips
9. Extraction Phase 7+ incomplete (outgoing_message, channel_id)
10. Bearer auth not systematically tested (2/117 endpoints)
11. Skill security scanner undertested (no adversarial corpus)
12. World-model migration consistency untested

### Low (Nice to Have)
13. README counts automated via CI
14. iMessage truncation byte vs char verification
15. E2E sleeps replaced with polling
16. TS integration tests against real OpenClaw SDK

---

*End of document. 16 actionable items total.*
