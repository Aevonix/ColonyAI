# Accepted local source drafts

An owner can associate a specific local comparison or summary with an existing
open commitment using `colony_accept_local_draft` or
`POST /v1/host/commitments/{id}/local-draft`. The acceptance names the question and
one to eight absolute UTF-8 source paths. The tool derives the owner, session and
turn from the actual transport; these are not model-selected arguments.

The host must explicitly select `COLONY_LOCAL_WORK_ENABLED=true` and one existing
Hermes no-agent/local job with `COLONY_LOCAL_WORK_JOB_ID`. Acceptance creates a
pending row in the existing initiative ledger. It neither activates the general
goal executor nor claims an in-memory dispatch. Without the selected host binding,
the route reports unavailable.

Each native fire selects at most one accepted initiative. Its actual canonical
execution ID and profile hash bind the assignment. The native pre-turn hook
acquires the existing commitment undertaking before any tools run. The task has
only its selected-source read tool and scoped native catalog access. Each read
delegates to Hermes `read_file`, checks the undertaking and current assignment,
and captures the source hash. Sources are limited to 64 KiB each and must be fully
read within the native 2,000-line bound. The task cannot modify inputs, run a shell,
delegate work, publish, or send a message.

An unverified report cites exact source handles and hashes. The report and receipt
are synced before the initiative is completed. A subsequent native fire reconciles
a saved receipt after a result-delivery interruption without repeating inference.
Unknown/running predecessors are left alone. Only classified transient failures
with a definitely failed native predecessor can consume the existing two-attempt
budget. Other failures remain failed with their reason and original history.

Pending, assigned and recent completed/failed work appears in the existing owner
current-work projection. A completed draft does not fulfil its broader commitment.
Cancellation of the initiative or parent obligation fences subsequent reads and
result acceptance. Generated task instructions are excluded from owner turn-memory
capture. Native session history and local draft files remain their own evidence.

This first class supports local text evidence only. It does not establish the
accuracy of model interpretation, complete arbitrary goals, or authorize any
external effect. The native runner accepts explicit host-selected runtime
parameters; private integrations should resolve their current planning role on
each fire, without coupling public code to deployment paths or model names.
