# Colony plugin for Hermes (official adapter)

The generic, deployment-agnostic Hermes plugin for Colony. It registers the native
`colony_*` tools (initiatives, queue, memory, briefings, world model, research,
autonomy bridge), a WebSocket event subscriber, slash commands, and a `pre_llm_call`
hook — all talking to a Colony sidecar over its `/v1/host/*` HTTP API.

This is an **optional integration**, not part of the harness-agnostic core. It lives in
this repo on purpose: co-locating the adapter with the API keeps their contract in
lockstep. `sidecar/tests/test_hermes_plugin_contract.py` asserts every endpoint the
plugin calls is a real route — the guard that would have caught the `/tasks` → `/initiatives`
drift that previously failed silently.

## Nothing here is deployment-specific

Persona, owner identity, and channels are **not** baked in. The autonomy bridge uses a
generic default prompt; a deployment injects its own via Hermes config:

```yaml
plugins:
  colony:
    url: http://127.0.0.1:7777
    api_key: ${COLONY_API_KEY}          # or via env
    autonomy_prompt: /path/to/persona_prompt.txt   # inline string OR a file path
    autonomy_deliver: dm                 # channel the bridge reports to (e.g. whatsapp)
```

If `autonomy_prompt` is omitted, the generic owner-agnostic prompt in
`_DEFAULT_AUTONOMY_PROMPT` is used. `autonomy_deliver` defaults to `dm`.

## Install

Copy this directory to `~/.hermes/plugins/colony/` (see `install.sh`) and enable it in
the Hermes config `plugins.enabled` list. Requires `httpx`, `websockets`, `pydantic`.
