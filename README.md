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

Pre-alpha. The endpoint contract is stable (additive `v1`); a few
backing endpoints still return `501 phase1_wiring_required`. The plugin
treats those as soft-fail and falls back to OpenClaw defaults.

See the full plan in the colony-ai repo at `docs/HOST_API.md` and
`/root/.claude/plans/radiant-bubbling-dolphin.md`.
