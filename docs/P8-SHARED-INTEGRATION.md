# P8 shared integration

Status: integrated into the Colony sidecar behind an explicit, default-off
shadow switch. It is an evidence collector and scoped context adapter, not an
action, approval, delivery, or identity authority.

## What is wired

When and only when `COLONY_RECIPIENT_SIMULATOR_MODE=shadow`, sidecar startup
attaches one process-owned runtime around the canonical `SharedFactsStore` and
three additive ledgers under `COLONY_STATE_DIR`:

- `colony-p8-visibility.db` — immutable, content-digest-bound fact envelopes;
- `colony-p8-arcs.db` — the existing append-only conversational arc store; and
- `colony-p8-recipient-audit.db` — reference/digest-only sample and evaluation
  receipts.

Unset, `off`, `live`, and unknown values all leave this integration off and do
not create those files. `live` deliberately remains dark in this slice. A P8
open or write failure cannot make the canonical SharedFacts write or existing
delivery result fail.

The host constructs `ViewerContextV1` only from middleware-produced scoped
request authority plus the person returned by Colony's server-side resolver.
Legacy global bearer, anonymous development authority, body channel/session,
prompt text, and model-provided scope fields cannot attest a P8 viewer. A
sender-resolving transport principal may bind a final participant only after
the server resolver has attributed the turn.

New fact writes at the manual SharedFacts and ToM extraction seams are split
into:

1. untrusted fact content and confidence; and
2. server-owned subject, exact `person:<subject>` scope, source receipt,
   freshness, and evidence.

The existing SharedFacts row remains canonical and unchanged. P8 writes a
versioned immutable envelope beside it. Context assembly rejoins the current
row to its exact envelope, constructs a typed candidate, and projects it
before relevance ordering or rendering. Legacy rows and changed rows without a
matching envelope remain absent; no migration invents authority for them.
Every context, relationship, Deck, Tom2-adapter, and simulation projection
applies the strictly positive `COLONY_P8_FACT_MIN_CONFIDENCE` floor (default
`0.5`; invalid or non-positive values fail safe to `0.5`). An internal caller
may only tighten this floor, never lower it. Relationship profiling stores a
contentless cache and derives rapport topics only at a request render boundary
using that request's sealed exact viewer. A viewer-bearing read re-projects
current facts, so legacy topics, deleted/expired facts, changed envelopes,
projection errors, and autonomy cache reads cannot become stored authority.

SharedFacts are also mirrored into the graph for ordinary semantic recall.
While P8 is attached, startup installs one graph-wide hard policy for the
current `tom:shared_fact` source URI and the legacy `shared_fact` metadata
marker. It applies during `recall()` and `read_memories()` hydration before
confidence/relevance ranking or reranking, so model tools, synthesis,
background thinkers, normal memory reads, and the research pipeline all share
the same boundary. Research borrows this configured graph and never creates or
closes a policy-free live client. Multimodal searches over the `memories`
collection boundedly oversample, hydrate ambiguous vector IDs against the
authoritative graph, filter, and then trim; non-graph image vectors remain
available. A host-side mirror filter remains as defense in depth. The typed P8
section is the mirrors' only content path; other graph memories retain their
existing exact-person recall behavior. If the governed startup graph is
unavailable while P8 is attached, research graph gathering and ambiguous
multimodal memory text fail empty; they never create a policy-free fallback
client.

Existing Tom2 context renderers receive a bounded `get_fact`/`list_facts`
adapter backed by current P8 projection. Unresolved legacy refs disappear
entirely—even the owner renderer does not expose their raw IDs or topology.

Older global context producers do not yet carry P8 visibility envelopes.
While P8 is attached, both `/context/assemble` and `/context/enriched` query
those sources only for a server-attested exact owner viewer. A guest or an
unsealed migration caller receives no global goals, initiatives, briefings,
world-model entities, insights, contact list, cognition snapshot, directives,
or surprises; the sources are not queried at all. The temporal endpoint and
assembled temporal block likewise keep owner-last-seen, global overdue
commitments, and other-contact cadence heads-up content owner-only while still
providing the current viewer's clock/timezone context. P8-off preserves the
legacy behavior. This is containment, not invented fact authority: exact
recipient memories and relationship context retain their existing scoped
paths, and the legacy global producers still need typed envelopes before they
can be shared with a non-owner.

The reasoning surface uses the same containment principle. Model-advertised
tool definitions are never treated as an execution gate: `ToolExecutor`
enforces the exact allowed-name set again, classifies unknown/dynamic tools as
mutations, and consults standing owner directives at dispatch. With P8 attached,
only a scoped exact owner can read private Colony/tool state, and mutations also
require `tools:mutate`; guests retain general calculation and web search. Body
contact claims and tool argument selectors cannot grant tool authority.

The non-real-time autonomy delivery seam constructs a detached, structurally
bounded value snapshot of the exact final sanitized text (or guard-cleared
revision), actual recipient, exact fact references, and target chat. Text is
not truncated: an oversize simulation request leaves an exact sample and
truthful incomplete coverage. The observer never receives transport-owned
mutable dictionaries. It appends the sample before evaluation.
The returned recommendation is ignored. It cannot mutate the draft/recipient,
spend an approval, authorize a recipient, block a delivery, call a transport,
or affect the existing return value. Observer errors are advisory and fail open
with respect to existing delivery.

## Explicit real-time exclusion

The integration is not imported or awaited by the host's custom voice turn engine,
phone system, intercom, barge-in, first-audio, Meet, or call-connect paths.
Voice-like transport aliases, including FaceTime, are rejected before an audit
sample is written.
There is no Hermes voice-path work in this integration. The deployed custom
voice system remains canonical and independently rollbackable.

## Read model

Three bounded/scoped endpoints support operations and an Operator Deck
adapter:

- `GET /v1/host/tom/p8/status`
- `GET /v1/host/tom/p8/deck?person_id=<exact-granted-person>`
- `GET /v1/host/tom2/report` (configured-owner viewer only while P8 is on)

All three require an authenticated scoped principal with `tom:read`. Legacy bearer
and cross-person selectors are refused. The Deck projection contains only
authorized fact content, reference-only visibility envelopes, authorized
active arcs, digest/reference-only simulation receipts, and coverage. Counts
are bounded by validated query parameters; denied content is never returned.
Audit receipts are owner-wide for the configured owner viewer and otherwise
require the recipient's exact recorded scope revision. Because autonomy
observations use an internal server scope, they are intentionally visible in
the owner Operator Deck rather than through an unrelated recipient credential.

`/mind/facts` remains the canonical raw management API for explicitly
authorized create/list/update/delete workflows; it is not a context/render
surface and this integration does not change its storage contract. Any caller
that turns those rows into model/user-visible content must use P8 projection.

## What remains intentionally unwired

- There is no live P8 advisory or enforcement mode.
- Arc storage is attached, but no canonical arc extraction/transition producer
  is introduced by this slice.
- Existing graph, world-model, goal, initiative, briefing, and surprise rows do
  not gain invented visibility envelopes; untyped global projections are
  therefore exact-owner-only while P8 is attached.
- Initiative producers do not yet consistently retain exact fact references;
  absent references are reported as provenance uncertainty, not fabricated.
- No ResponseGuard semantics, SharedFactsStore schema/behavior, host-deployment code,
  Hermes code, voice code, Meet code, or live service configuration changes.

## Verification

Run from `sidecar/` in the pinned source checkout:

```bash
python -m pytest -q \
  tests/test_tom_p8_visibility.py \
  tests/test_tom_p8_visibility_store.py \
  tests/test_tom_p8_fact_adapters.py \
  tests/test_tom_p8_arcs.py \
  tests/test_tom_p8_recipient_simulator.py \
  tests/test_tom_p8_recipient_audit.py \
  tests/test_tom_p8_server_integration.py \
  tests/test_tom_p8_outbound_integration.py \
  tests/test_relationships.py \
  tests/test_recall_person_boost.py \
  tests/test_tool_handlers_graph.py
```

See `docs/runbooks/P8-SHARED-INTEGRATION-CANARY-ROLLBACK.md` for deployment,
local-install rollback, and evidence collection.
