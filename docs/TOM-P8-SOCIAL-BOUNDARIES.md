# P8 fact-level social boundaries and conversational arcs

Status: core primitives plus a default-off, shared Colony shadow integration.
See `docs/P8-SHARED-INTEGRATION.md`; live enforcement and every real-time voice
path remain unwired.

## Outcome

P8 adds three small, generic Colony primitives:

- `tom/visibility.py` defines an immutable, digest-bound fact visibility
  envelope and filters fact content before ranking, prose generation, or
  recipient simulation.
- `tom/arcs.py` stores conversational arc events in an append-only SQLite
  ledger and deterministically reduces them into scoped active arcs.
- `tom/recipient_simulator.py` evaluates a bounded outgoing draft against only
  recipient-authorized facts and arcs, returning reference-only risks, repair
  suggestions, and an audit digest.

These primitives do not authenticate identities, send messages, approve
actions, write memories, call a model, or grant authority. The deployment must
derive `ViewerContextV1` from authenticated transport and identity state. A
model-provided person, scope, shareability, or attestation is untrusted input.

## Fact visibility contract

`FactVisibilityV1` binds one fact reference and exact content digest to:

- source and bounded evidence references;
- subject person;
- exact viewer scope and shareability class;
- confidence;
- observed time and an explicit freshness deadline.

The record is frozen. A content or audience change requires a new record and
therefore a new digest. Supported pairings are exact:

| Shareability | Valid viewer scope | Non-owner access |
|---|---|---|
| `owner_private` | `owner` | none |
| `subject_private` | `person:<subject>` | exact subject |
| `shared` | exact person, conversation, or `audience:shared` | exact attested match |
| `public` | `public` or `audience:global` | matching public/global scope |

`project_facts()` applies identity, freshness, confidence, conflict, count,
and character bounds before exposing content. Unknown identity, stale facts,
low confidence, and conflicting content under one fact reference fail closed.
An empty scoped result remains empty; there is no global retry.
The shared integration supplies a positive floor to every projection via
`COLONY_P8_FACT_MIN_CONFIDENCE` (default `0.5`; invalid/non-positive fails safe
to the default). Per-call overrides can only raise that floor.

## Conversational arc contract

`ArcStore` persists immutable `ArcEventV1` events. Its SQLite triggers reject
updates and deletes. Event and idempotency identifiers are unique; exact replay
returns the original event, while changed replay raises `ArcConflictError`.

The reducer supports:

- promises;
- open questions;
- stress topics;
- decisions;
- shared plans;
- follow-ups;
- unresolved social moments.

An open event fixes type, topic, people, and viewer scope. Every later link or
transition event carries its own subject/shareability/viewer envelope. The
reducer accepts it only when that envelope exactly matches the open event and
the event subject is one of the arc's linked people; differently scoped private
references can never be merged into the visible projection. Valid events may
link turns, people, commitments, expectations, projects, and evidence, or make
a state transition. A deadline can make an active arc overdue, but cannot close
it. Close and cancel events require explicit evidence and a reason.
Post-terminal events are rejected unless they explicitly reopen the arc with
evidence.

Projection is active-only, recipient-scoped, deterministic, and bounded. A
corrupt history is counted but never returned.

## Recipient simulation contract

`COLONY_RECIPIENT_SIMULATOR_MODE=off|shadow|live` defaults to `off`; an unknown
value is also `off`.

- `off` does not query facts or arcs and returns `no_effect`.
- `shadow` evaluates but always returns `observe_only` as its effective action.
- `live` returns a pre-send advisory action for non-real-time message callers.

Every result sets `external_effect=false`, `authority_granted=false`, and
`synchronous_gate=false`. It contains fact and arc references, stable risk and
repair codes, projection digests, and an overall audit digest—not draft text,
fact content, dependency errors, credentials, or host topology.

Dependency behavior is explicit:

| Caller risk class | Dependency-failure advisory |
|---|---|
| `low` | `observe` |
| `medium` | `review` |
| `high` | `hold` |
| `critical` | `hold` |

An unattested recipient is not used to query either dependency. A referenced
fact absent from the authorized projection is critical and yields a would-hold
result. High-salience prose without fact references is marked for provenance
review. The first deterministic social rule detects pressure language that
overlaps an active recipient-authorized stress topic and suggests softer
language.

### Real-time voice exclusion

`voice`, `phone`, `intercom`, `meet`, `facetime`, and compound/transport aliases
such as `google_meet`, `phone_call`, `apple_facetime`, `sip`, `pstn`, and
`webrtc` are permanently
asynchronous in this core: even in `live`, their result is `observe_async` with
`evaluation_path=async_observation` and `synchronous_gate=false`. P8 must not be
inserted into the custom host voice turn path, barge-in path, first-audio path,
or call-connect path. Voice may submit a completed-turn observation later for
learning and audit. The working voice system remains canonical and unchanged.

## Shared integration seams and future producers

The shared integration implements the identity boundary, versioned SharedFacts
envelopes, outbound shadow observation, audit persistence, and bounded read
model below. The broader producers remain future work:

1. **Identity boundary (implemented):** `context_assemble` and
   `enriched_context` construct `ViewerContextV1` only after
   `resolve_request_person()` returns the server-authorized person. Principal,
   revision, and audiences come from request authority; body channel/session
   and model output grant nothing. Relationship detail and context rapport
   rendering use the same request-sealed exact viewer; autonomy/cached briefs
   contain no P8-derived topic content.
2. **Fact envelope producer (SharedFacts implemented):**
   `_run_tom_extraction`, manual extraction, create, and update append an
   immutable visibility record with server-derived subject and source receipt.
   Graph, world-model, goal, initiative, briefing, and surprise producers still
   need their own typed candidates.
3. **One arc writer (store implemented; producers future):** startup attaches
   one `ArcStore`. Commitment, expectation, project, and extraction seams do
   not emit canonical arc events yet, and a prose model may never close an arc
   without a source receipt.
4. **Outbound provenance (consumer implemented; producers incomplete):** the
   observer consumes exact optional fact references carried by the initiative,
   after `DeliveryBridge.preview_initiative()` resolves the actual recipient.
   Draft producers still need consistent reference propagation.
5. **Messaging observation (implemented only in shadow):** non-real-time final
   sanitized/revised text is copied into a detached bounded snapshot, sampled,
   and evaluated, but the result is ignored and has no ResponseGuard or
   delivery authority. The observer cannot mutate the live message or target.
6. **Durable audit and Deck read model (implemented):** the append-only,
   digest-only ledger and scoped bounded endpoints expose would/effective
   action, risk/repair codes, scope revision, references, and coverage without
   denied content.
7. **Real-time surfaces (explicitly excluded):** there is no host-deployment, Hermes,
   phone, intercom, FaceTime, Meet, or voice observation adapter in this
   integration and no real-time caller invokes or awaits P8.

## Deliberately unresolved gaps

This core is not the full P8 graduation:

- graph, world model, goals, initiatives, briefings, and surprises do not yet
  emit versioned fact candidates;
- there is no canonical arc extraction/classification producer;
- outbound draft producers do not yet consistently retain the exact fact
  references used;
- only one conservative social repair rule exists; affect, timing,
  relationship state, promises, and likely-interpretation rules remain future
  shadow work;
- `live` remains unwired and maps to off in the shared integration; P8 has no
  ResponseGuard authority and cannot block, allow, mutate, approve, or send.

Until these gaps are closed and shadow evidence is reviewed, keep the mode
`off` or `shadow`; do not describe P8 as a live delivery gate.

## Verification evidence

The P8 boundary suite covers immutability, freshness, unknown identity,
cross-person leakage, bounded projection, append-only triggers, evidence-bound
closure, replay, restart, concurrency, dependency failure, no-effect mode, and
voice async exclusion. See the migration runbook for the commands and rollout
criteria.
