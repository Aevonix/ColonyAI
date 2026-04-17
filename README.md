# @aevonix/colony-core

Colony's intelligence sidecar — **graph memory, autonomy loop, context assembly, safety pipeline** — mountable into any agent harness via the `/v1/host` API.

Currently ships adapters for **OpenClaw** (first supported host). Future releases will add adapters for Hermes and other agent harnesses. The sidecar client and shared helpers are host-agnostic by design.

## What it registers

| OpenClaw extension slot | Colony adapter | Status |
|---|---|---|
| `registerMemoryCapability` | `MemoryPluginCapability` with `promptBuilder` + `MemoryPluginRuntime` (search, read, flush, status, probe) | Working (gated by `ownMemoryCapability` config flag; exclusive slot) |
| `registerMemoryEmbeddingProvider` | `MemoryEmbeddingProviderAdapter` with `create()` factory pattern | Working (returns `{provider: null}` when sidecar has no embedder) |
| `registerContextEngine` | `ContextEngine` with `info`, `ingest` (no-op), `assemble`, `compact` (delegated) | Working |
| `registerAgentHarness` | `AgentHarness` with `supports`, `runAttempt`, `reset`, `dispose` | Wired but reasoning endpoint is 501 until Stage B lands |
| `api.on("message_sending")` | Safety gate with fail-closed policy | Working (fires per outbound chunk) |
| `api.on("reply_dispatch")` | Post-turn cognition sync → `signals/ingest` + `turns/sync` | Working (fire-and-forget observer) |
| `registerService` | Events lifecycle — WS subscriber with first-message auth + diagnostic logging | Working |

All adapter shapes match the real OpenClaw SDK contracts. Zero `@ts-expect-error` markers.

## Configuration

```jsonc
// In your OpenClaw config:
{
  "plugins": {
    "entries": {
      "colony": {
        "config": {
          "sidecarUrl": "http://127.0.0.1:7777",   // colony-core server
          "apiKey": "sk-colony-...",                  // colony API key
          "ownReasoningLoop": false,                  // opt-in: Colony drives reasoning
          "ownMemoryCapability": false,               // opt-in: Colony owns the memory slot
          "failSafetyClosed": true,                   // block outbound on safety errors
          "forwardProactiveDeliveries": true,          // subscribe to events WS
          "hostId": "openclaw",                        // identity for audit
          "requestTimeoutMs": 30000                    // per-call HTTP timeout
        }
      }
    },
    "slots": {
      "contextEngine": "colony"                       // activate Colony's context engine
    }
  }
}
```

### Config reference

| Key | Type | Default | Description |
|---|---|---|---|
| `sidecarUrl` | `string` (URL) | `http://127.0.0.1:7777` | Colony-core sidecar base URL |
| `apiKey` | `string` | (required) | Colony API key (`sk-colony-...`) |
| `ownReasoningLoop` | `boolean` | `false` | Register Colony as the active agent harness (Stage B) |
| `ownMemoryCapability` | `boolean` | `false` | Register Colony as the exclusive memory capability (claims the slot from memory-core) |
| `failSafetyClosed` | `boolean` | `true` | Block outbound messages when safety sidecar is unreachable |
| `forwardProactiveDeliveries` | `boolean` | `true` | Subscribe to `/v1/host/events` WebSocket for autonomy-loop events |
| `hostId` | `string` | `"openclaw"` | Identity reported to colony-core for audit/scoping |
| `requestTimeoutMs` | `number` | `30000` | Per-call HTTP timeout (ms) |

## Quick start (OpenClaw)

```bash
# 1. Start colony-core sidecar
cd colony-ai && python run_server.py

# 2. Install into OpenClaw
cd your-openclaw-instance
pnpm add @aevonix/colony-core

# 3. Add config (see above) to your OpenClaw config file

# 4. Start OpenClaw — Colony adapter loads automatically
```

## Development

```bash
pnpm install
pnpm test          # 116 unit tests
pnpm typecheck     # zero errors

# Integration tests (requires a running colony-core sidecar)
COLONY_SMOKE_URL=http://127.0.0.1:17777 pnpm test:integration
```

## Architecture

```
+----------------------------------------+        +-----------------------------+
|  OpenClaw (Node/TS host)               |        |  colony-core (Python)       |
|                                        |        |                             |
|  +----------------------------------+  |  HTTP  |  + AutonomyLoop (17-phase)  |
|  | @aevonix/colony-core             |<--------->|  + CognitionPipeline        |
|  |  - MemoryPluginCapability        |  |  WS    |  + Graph memory (Neo4j)     |
|  |  - MemoryEmbeddingProvider       |  |        |  + LanceDB vector store     |
|  |  - ContextEngine                 |  |        |  + Mind model + signals     |
|  |  - AgentHarness (opt-in)         |  |        |  + Relationship scorer      |
|  |  - Safety hook (fail-closed)     |  |        |  + 7-layer ResponseGate     |
|  |  - Reply-dispatch observer       |  |        |  + Goal/Initiative engine   |
|  |  - Events lifecycle service      |  |        |  + Proactive delivery       |
|  +----------------------------------+  |        |  + Skills registry/executor |
+----------------------------------------+        +-----------------------------+
```

## Known limitations

- **Reasoning endpoint is 501** — Colony's `AIAgent.run_conversation` hasn't been extracted from the legacy `run_agent.py` yet (Stage B work). The harness adapter is wired and `supports()` gates correctly, but `runAttempt` always returns `promptError` until colony-core advertises the `"reasoning"` capability.
- **Post-turn extraction fields are empty** — `PluginHookReplyDispatchEvent` doesn't carry assistant text, topics, entities, or tools used. Colony gets these eventually via its own signal ingestion, but the `turnsSync` call ships with empty extraction fields for now.
- **Safety hook `incoming_message_text` is empty** — needs a companion `inbound_claim` hook to cache the incoming text per session. Currently sends `""`.
- **Identity surrogate** — `contact_id` is derived from `sessionKey ?? sessionId ?? event.to` depending on the hook. Colony's cognition layer works best with a real person identifier; the surrogate is functional but degenerate for multi-person deployments.
- **No multimodal embeddings** — `embedBatchInputs` is omitted; colony-core's embed endpoint accepts text only.

## License

MIT
