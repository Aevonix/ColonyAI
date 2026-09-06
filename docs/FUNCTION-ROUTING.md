# Function routing on the existing model pool

Colony's shared `LLMRouter` now selects a named function and tries only that
function's eligible local candidates. It keeps its object identity when host
configuration changes, so retained extractors, thinkers, planners and workers
see updates. An active request retains its old model, endpoint, credentials,
capabilities and fallback order until that request finishes. A later request
uses the new configuration.

Successful responses include `prior_attempts`: earlier binding/model names,
`failed` versus `skipped` status, and exception class or cooling-down reason.
Endpoint addresses, credentials and exception messages are omitted. Ineligible
candidates are not attempted and do not appear as failures. The returned model
and binding remain the actual processor; returning a fallback alone does not
prove that every earlier configured candidate was attempted.

This packet covers OpenAI-compatible chat completions for `chat`, `reasoning`,
`planning`, `extraction`, `judging`, `vision` and `coding`. It does not replace
Hermes's primary conversation routing, voice/STT/TTS transports, embeddings or
reranking. Those remain separately configured consumers. There is no new
service, fleet scanner, workflow engine, cloud authorization path or automatic
model benchmark.

## Configuration

The existing state-directory `.colony-llm-config.json` remains authoritative.
Existing `provider`, `baseUrl`, `apiKey` and `models.small/medium/large` bindings
work for supported local OpenAI-compatible endpoints. Object specs may declare
`supportsTools`, `supportsVision`, `contextTokens`, `latencyMs`,
`tokensPerSecond`, `concurrency` and `weightRevision`. Existing
`usefulContextTokens` remains a compatible context hint.

The optional `modelPool` provides more than three named bindings, independently
of machines or parameter count. `functionRoles` chooses ordered candidates and
requirements. Names express deployment roles, not hardware identifiers.

```json
{
  "provider": "local",
  "apiKey": "local-no-key",
  "models": {},
  "modelPool": {
    "interactive": {
      "model": "deployed-fast-model",
      "baseUrl": "http://10.0.0.20:8000/v1",
      "supportsTools": true,
      "contextTokens": 64000,
      "maxTokens": 4096,
      "latencyMs": 250,
      "tokensPerSecond": 80,
      "concurrency": 4
    },
    "deliberate": {
      "model": "deployed-reasoning-model",
      "baseUrl": "http://10.0.0.21:8000/v1",
      "supportsTools": true,
      "contextTokens": 128000,
      "maxTokens": 8192,
      "latencyMs": 2000
    },
    "visual": {
      "model": "deployed-vision-model",
      "baseUrl": "http://10.0.0.22:8000/v1",
      "supportsVision": true,
      "contextTokens": 64000
    }
  },
  "functionRoles": {
    "chat": {"candidates": ["interactive"], "maxLatencyMs": 500},
    "reasoning": ["deliberate", "interactive"],
    "planning": ["deliberate", "interactive"],
    "extraction": ["interactive", "deliberate"],
    "judging": ["deliberate"],
    "vision": ["visual"],
    "coding": ["deliberate", "interactive"]
  }
}
```

Every public example is neutral. Real credentials belong only in private host
configuration. `modelPool` supports at most 64 bindings and each role at most
eight candidates. A candidate spec may override the inherited `apiKey`,
`baseUrl`, `maxTokens` and `extraBody`. A `tier` is optional legacy metadata;
the selected function is reported separately.

A role object accepts `minContextTokens`, `maxLatencyMs`, `minTokensPerSecond`,
`minConcurrency`, `timeoutSeconds` and `deadlineSeconds`. Capabilities and
performance figures are deployment declarations, not router measurements.
Unknown values do not satisfy explicit constraints. Selection uses declared
order after filtering, without guessing intelligence from a model name.
Concurrency describes advertised capacity; this packet does not reserve slots
or measure current contention. Independent registration can update this same
configuration later.

Without explicit function mappings, chat uses SMALL; extraction uses SMALL then
MEDIUM; reasoning/planning use LARGE, MEDIUM, SMALL; judging/coding use LARGE
then MEDIUM; vision uses only explicitly configured VISION. Missing model
bindings are omitted. Provider presets and discovered file size do not invent
available function candidates.

Explicit pool candidates need `supportsTools: true` for tool-bearing requests.
Existing tier bindings preserve unknown tool capability for compatibility;
status marks these `legacy_unknown_tools_allowed: true`. After a real successful
tool exercise, the deployment can replace the unknown with an explicit
capability. There is no unknown-capability exception for vision.

Only declared OpenAI-compatible transport is supported by this function path.
An existing Ollama deployment must explicitly declare `protocol: "openai-chat"`
and provide its compatible `/v1` URL, or configure that compatible endpoint as
`provider: "local"`. Native Ollama or other provider-specific protocols are
rejected during configuration; they are not accepted as unusable candidates.
The older direct-construction tier router API remains for its existing callers.
No Colony consumer currently requests router streaming. `stream=True` is
rejected before a function call, and Hermes retains its own streaming behavior.

## Reload and fallback

Calls and metadata reads check the configuration file's inode, size and mtime.
A changed file is parsed and validated into a separate snapshot, then published
atomically. Invalid or partial edits retain the last valid snapshot and expose
`reload_error`; an invalid first configuration leaves inference unavailable,
with no implicit provider default. Configuration files should be replaced
atomically. `POST /v1/host/configure` validates, writes a private file atomically,
and updates the same router object while preserving the existing reasoning
loop and tool executor.

The default timeout is 20 seconds per interaction/extraction/image candidate,
with a 40-second total deadline. Reasoning/planning/judging/coding default to
120 seconds per candidate and 180 seconds total. Operators may adjust these
bounded limits per role. The existing source assertion and image workers also
have their own 40-second outer bounds. A timeout, connection failure, rate limit,
context-window exception, completion 404 or transient server error can try the next eligible
candidate. Authentication, validation and redirects do not trigger blind
retry. The same endpoint/model/weight generation is not retried under another
legacy alias. `allow_fallback: false` restricts a call to its first candidate.
Explicit legacy `force_tier` remains exact and cannot bypass eligibility.

A failed endpoint/model binding now cools down for 15 seconds, so subsequent
requests can use an eligible fallback immediately. After that interval, one
request attempts recovery while concurrent requests keep using their eligible
fallbacks. A successful completion restores the primary. Cancellation releases
the recovery slot. If no eligible fallback is available, the request reports
that fact. Updating the endpoint or model configuration starts new observations
without a restart; an in-flight call still retains its original snapshot.

A fallback must retain the request's modality and configured capability
constraints. The existing text token estimator also excludes obviously
undersized declared contexts, including the requested output allowance. This
is a heuristic, not exact tokenizer accounting; image token usage and unknown
legacy context sizes remain unknown. The existing queue context gate reads
function configuration when a named pool has no corresponding legacy tier.

All automatic function candidates must be local. Default networks are loopback,
RFC1918 IPv4 and IPv6 unique-local ranges. `localNetworks` can replace these with
deployment CIDRs. A hostname must also appear in `localHosts`, and every resolved
address must fall within the configured networks. The resolved address is
pinned for that request. A friendly hostname suffix is insufficient. Requests
use explicit endpoint credentials, bypass environment proxy configuration and
do not follow redirects. Model endpoints themselves remain deployment-trusted
services. A prompt or `allow_cloud` hint cannot authorize cloud fallback; a
separate explicit authorization policy would be needed for that behavior.

A hostname with an unavailable address family can try its other verified
addresses after a connection failure. At most eight unique resolved addresses
are considered under the same candidate deadline; authentication and validation
responses do not cause address retries. This also applies to model discovery.

## Observable behavior and provenance

`GET /v1/host/models` observes the explicitly configured pool through each
endpoint's OpenAI-compatible `/v1/models` route. Both a server root and an
existing `/v1` base are normalized correctly. Reads reuse observations for 30
seconds and probe at most four endpoints concurrently, with a two-second bound
per read. Additional reads cover larger pools, starting with unobserved or
oldest endpoints. No timer, scanner or background service is added.

Routing status separates model advertisements from actual completion outcomes.
It includes advertised model IDs and context limits, observation timestamps and
ages, stale flags, inventory completeness, completion latency, served model ID
and cooldown/recovery state. An advertisement never grants tool or vision
support or changes declared context and performance constraints. A configured
request alias can work even when absent from the server's listing. Only an
actual failed completion affects availability. A missing listing therefore
remains an observation failure without disabling working inference.

Source assertions now request `extraction`, image descriptions request `vision`,
and project plans request `planning`. Existing named task hints map compression
and ToM extraction to `extraction`, working-state thinking to `reasoning`, skill
distillation to `judging`, and tool drafts to `coding`. Other router calls default
to `reasoning`; this does not alter Hermes's separately selected chat model.

Each successful response names its function, selected binding/model,
configuration revision and `weightRevision`. Missing immutable model revisions
remain `unknown`. The configuration fingerprint identifies supplied config,
not model weights or an attestation. Assertions retain these fields in their
existing derived JSON. Image descriptions retain them in their existing media
row. Reasoning results pass them into existing operational outcome evidence.
`GET /v1/host/models` includes routing metadata and up to 20 recent successful
call traces without prompts, endpoint URLs or credentials. These recent traces
are process-local diagnostics; persisted source/operational evidence remains
the durable provenance.

Qualification uses real disposable HTTP endpoints for failures, candidate
constraints, declared-host resolution, in-flight reload, retained router
references, source assertion projection and image caption storage. Two neutral
actual-LAN calls also used the same retained router before and after a temporary
configuration change; the second recovered from a closed local endpoint and
selected the new binding. This is functional qualification, not an intelligence,
latency population or extraction-quality benchmark. Production adoption still
requires a real deployment config change and observed downstream behavior.
The endpoint observation extension also exercises URL normalization, working
unlisted aliases, completion 404/503 fallback, concurrent recovery, cancellation,
cache expiry, pool coverage and an endpoint move through atomic config reload.
