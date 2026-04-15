# @aevonix/openclaw-colony

OpenClaw plugin that mounts [Colony](https://github.com/aevonix/colony-ai)'s
intelligence — graph memory, the seven-layer safety pipeline, the
17-phase autonomy loop, theory-of-mind signals — into any OpenClaw
install.

The plugin itself is a thin TypeScript shim. It speaks the
`/v1/host/*` HTTP/WebSocket contract exposed by **`colony-core`** (the
Python sidecar carved out of `colony-ai`'s intelligence stack) and
forwards each call to the matching OpenClaw plugin extension slot.

## What it registers

| OpenClaw slot                       | Backed by                  |
|-------------------------------------|----------------------------|
| `registerMemoryCapability`          | colony-core graph memory   |
| `registerMemoryEmbeddingProvider`   | colony-core embeddings     |
| `registerContextEngine("colony")`   | colony-core context engine |
| `registerAgentHarness` (opt-in)     | colony-core reasoning loop |
| `registerHook(["message_sending"])` | seven-layer ResponseGate   |
| `on("reply_dispatch")` (post-turn)  | SignalCollector            |
| `registerService` (background)      | host events WebSocket      |

## Configuration

```json
{
  "plugins": {
    "entries": {
      "colony": {
        "module": "@aevonix/openclaw-colony",
        "config": {
          "sidecarUrl": "http://127.0.0.1:7777",
          "apiKey": "sk-colony-...",
          "ownReasoningLoop": false,
          "forwardProactiveDeliveries": true
        }
      }
    }
  }
}
```

`ownReasoningLoop` is off by default. Until Phase 1 of the colony-core
plan lands the experimental `registerAgentHarness` path, OpenClaw
should drive the reasoning loop and Colony provides memory + safety +
signals only.

## Status

**Pre-alpha. Adapter shapes do not yet match the real OpenClaw SDK
contracts.** The `registerMemoryCapability`, `registerMemoryEmbeddingProvider`,
`registerContextEngine`, and `registerAgentHarness` objects this plugin
returns are scaffold shapes, not the SDK-compliant shapes. OpenClaw's
runtime will either silently no-op on the registration or crash on
first invocation. The safety hook is wired via `registerHook` but the
event shape also doesn't match `PluginHookMessageSendingEvent`.

What IS correct and ready to build on:

- `ColonySidecarClient` — typed one-method-per-endpoint HTTP/WS client
  over `/v1/host/*`, including first-message auth handshake for the
  events WebSocket.
- Shared helpers: `withDegradation`, `capabilityProbe` (with
  `hasProbedSuccessfully()` for distinguishing "sidecar says no" from
  "probe failed"), `summarizeHostEvent`, and `ColonyEmbedUnavailableError`.
- Unit test coverage of those helpers (see `tests/helpers.test.ts`).
- `pnpm-lock.yaml` pinned for reproducible builds.
- The endpoint contract itself is stable (additive `v1`); endpoints
  that aren't wired return `501 phase1_wiring_required` and the
  helpers above treat those as soft-fail.

What's blocking a real release:

- Rewire each adapter against the real SDK contracts in
  `openclaw/plugin-sdk`. Tracking issue: see the colony-ai repo
  issues labelled `plugin-adapters`.
- Replace the structural `OpenClawPluginApi` stub in `src/plugin.ts`
  with `import type { OpenClawPluginApi } from "openclaw/plugin-sdk/
  plugin-entry"` so `tsc` enforces the contracts.

See the full plan in the colony-ai repo at `docs/HOST_API.md` and
`/root/.claude/plans/radiant-bubbling-dolphin.md`.
