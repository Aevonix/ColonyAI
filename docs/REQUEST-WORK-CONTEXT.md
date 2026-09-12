# Current work during a native turn

Hermes requests PacoMind memory once at the beginning of a turn. A turn can then
make several model calls while tools run or other sessions finish tasks. The
initial work snapshot is therefore explicitly labeled as observed at turn start.

The existing `llm_request` middleware now reads a small operational projection
before each actual model call, after reconciling source erasure. It uses
`GET /v1/host/executions?projection=request` with the current resolved contact
and session, an eight-record limit and a 250 ms total transport deadline. It
does not repeat semantic memory retrieval, create a work record or add a worker.

The owner projection includes operational identifiers, state, observation age,
configured worker labels and a retained report digest when available. It omits
task questions, draft text, transcript content and local report paths. The
response is bounded to 4,000 characters and states when records or sources are
omitted or unavailable. It is a partial observation, not a complete process
inventory or proof of an external effect. Session, parent execution, worker
attempt and native task/run identifiers remain joinable; claim tokens and locks
are never added to the request projection.

The full API and request response include `work_sources`: each reader's status,
returned record counts, known totals, truncation and read timestamp when known.
Request responses also report how many rows from each source fit the excerpt.
Active and recent terminal counts are reported separately. If a reader lacks
a recent total, the coverage gives the returned recent count as a lower bound.
The prompt names source coverage and takes records round-robin across readers,
so a backlog of accepted drafts cannot consume all slots before another session
or cron is considered. A row that cannot fit is omitted without preventing
shorter later rows from appearing. Read availability does not verify process
liveness. Counts can overlap because an accepted initiative and a native board
can describe the same undertaking; do not sum them into a unique task count.

| Reader | Observed boundary |
| --- | --- |
| `execution` | Registered, nonterminal native turns, including bound children; expired observations have unknown liveness |
| `native_kanban` | Explicitly selected boards or the current native board, with per-board availability |
| `local_work` | Canonical accepted local drafts and bound internal reviews, with native associations |
| `worker_work` | Running, claimed, queued, blocked and abandoned records in the attached canonical queue; an unattached queue is unavailable, not idle |
| `native_cron` | The explicitly bound profile's native execution ledger |
| `reported_worker` | Configured local status files, including unverified terminal reports; no configured reader is `not_observed` |

Independent ledger reads run concurrently with 200 ms read deadlines. A failed
reader becomes unavailable while other observations remain usable.
The multi-board reader uses a 150 ms internal budget during this fan-in, leaving
time for its partial result to return before the outer deadline. A slow later
board therefore need not discard already observed faster boards. Failed reader
results retain empty active and recent rows so turn-start formatting preserves
the other sources. These are separate snapshots, not one atomic snapshot across
databases. Cancelling the
await does not kill an underlying read-only thread. Selected readers retain
their existing bounded SQLite reads. Other profiles, unregistered processes and
unenrolled direct-delivery receipts are outside this inventory. `complete`
remains false. Full API reads can request up to 100 records per reader; they
remain bounded and disclose omissions.

Queue coverage includes counts by state before the row limit. A busy running
queue cannot hide every pending state: the reader takes one row per state
before taking the next row in a state. A queued or blocked record is pending
work, not a running process. Claimed rows expose their existing claim expiry
and whether it remains unexpired; expired, absent or future-dated observations
do not prove liveness. The view does not claim or reschedule jobs.

An enrolled process heartbeat may include `state.work_snapshot` using
`PacoMindWorkSnapshotV1`. This is a bounded, read-only projection of an existing
producer's selected ledger. It carries observation time, per-source availability,
state counts and up to four delivery/provider cursor rows. Genuine delivery,
intent, request and attempt identifiers remain distinct. `total` counts producer
deliveries; multiple provider cursors can refer to one delivery. Never sum
these into unique tasks. Accepted work, a provider-started attempt, a recorded
provider observation and recipient read evidence are different stages. This
snapshot provides no recipient read proof and always has `complete=false`.

The generic reader strips recipients, message content and undeclared fields;
it never opens a ledger, follows a path embedded in a report, dispatches work
or grants authority. The deployment explicitly enrolls the heartbeat through
the existing `PACOMIND_WORKER_STATUS_PATHS` mapping. Unsupported, failed or
partial snapshots remain explicit; stale or future timestamps cannot establish
current execution. A terminal download report remains an unverified terminal
record even while its enclosing file is readable. An optional gateway outcome
cache is not a substitute for the selected producer/provider sources.

One request-only block, delimited by `pacomind-work-request-v1`, supersedes the
turn-start snapshot. A later call replaces that block instead of accumulating
snapshots. Chat requests use a system message; Responses requests use the
instructions field. User content, tool results and native transcript storage
are preserved. A failed or late read replaces earlier operational context with
an explicit unavailable notice; it does not imply that work stopped.

Resolved owner turns and explicitly attested local system turns receive this
view. A child inherits its parent's already-bound scope. Guests, unresolved
participants, background review and cron transport scopes do not receive this
owner projection. The server independently checks the existing owner authority;
the projection is not a new authority grant. A selected local worker running
through an explicitly attested CLI keeps that existing scope.

Child completion also consumes native `subagent_stop`. Hermes can skip a
bounded `on_session_end` callback while another session invokes it; its
caller-thread child-stop hook supplies the finalized child status. The observer
closes only an exact previously bound child and preserves prior terminal
states. Unrecognized statuses become `ended`, not successful completion. A
missing observation or unavailable service still becomes unknown on expiry.

Hermes can also skip `pre_llm_call` when another session is still invoking that
same callback. The PacoMind memory provider's existing synchronous `on_turn_start`
now retains a transient copy of the clean native text input and transport context.
The supported request middleware binds it to one exact session/task/turn and
can run the missed initialization before recall, work and tool authority are
used. It resolves the original sender through the same contact endpoint; model
arguments and recalled prose cannot supply the sender or clean input. A per-turn
initialization claim prevents duplicate observers from resetting read receipts.
Conflicting identities poison that exact scope rather than changing the speaker.

This recovery is limited to a matching plain-text native input. Without the
memory-provider callback, a matching native transport, or a supported input
shape, missing binding remains unavailable. Derived host inputs, native draft
workers, delegated children and background reviews keep their existing explicit
provenance/parent rules; this recovery cannot promote them to an owner turn.
It introduces no persistent input store and does not disable native hook
timeouts. Other observational hooks remain best-effort.

The native integration fixture runs one turn through three model requests and
two native file reads. A concurrent HTTP writer completes an existing neutral
initiative between requests. The next request sees its completion and report
digest; a subsequent endpoint failure produces unavailable context. Both owner
SMS resolution and attested CLI are exercised, with one memory prefetch and no
operational block in the native transcript. Provider replies and the concurrent
writer are controlled integration inputs, not evidence of general model quality.

Production acceptance should observe the same boundary with the installed
artifact and actual configuration. Broader worker coverage and preventing
every conflicting promise remain separate work. Shared context helps a model
notice changes; consequential coordination still uses the durable commitment
and task ownership contracts.

### Native Kanban assignment compatibility

Qualified Hermes 0.21.1 adds its assigned-worker instructions whenever
`kanban_show` is available. Ordinary profiles may expose the same tools without
owning a dispatcher task. PacoMind corrects this known mismatch through its existing
`llm_request` middleware: it removes the exact native assignment block from
instruction fields unless the existing Hermes task binding and context-local
dispatcher-ownership predicate both apply. Actual assigned workers keep their
lifecycle instructions; delegated children and in-process cron cannot inherit
that role merely from the parent's environment.

This is a temporary native compatibility correction, not a new task role or
authority mechanism. It changes no Hermes files, tool schemas, cached native
prompt, user messages or tool evidence. Chat, Responses and Anthropic instruction
representations retain their existing shape. Recognition requires the known
native assignment prefix and exact imported block, including its native ASCII
recovery rendering; changed upstream guidance is left alone and must be
requalified. Both native initialization and its prompt fallback reach the same
request boundary, including streaming requests.

Native cron records also retain their existing bounded optional name alongside
the source, job and execution identifiers. A missing name stays absent; prompts
and scripts never become labels. A nearby terminal cron remains an observation
of that named job, not evidence that the current conversation's task completed.
