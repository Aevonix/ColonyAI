# Remembering useful tool observations

PacoMind can retain the original result of an explicitly nominated tool call for
later automatic recollection. The ordinary agent can use
`pacomind_memory_retain_observation(call_id, reason)` after a tool result supplies
concrete information with future value. The owner does not need to invoke the
memory tool by name. The reason explains why the result is useful; it is not a
replacement for its contents.

After eligible completed calls appear in the current request, the adapter adds
a short request-only hint with their exact call IDs, tool names and bounded
execution-argument previews. Calls using the same tool can be distinguished by
what they actually requested. A truncated preview is explicitly marked; check
the original call when the preview is insufficient. It appears
only when the retention tool is directly available or explicitly listed in the
native deferred catalog with its describe/call bridge. This also makes IDs
readable when a model's chat template omits API call metadata. The hint lists at
most eight candidates, newest first, within 2,048 characters including the hint
wrapper; it contains no tool
result text and does not say anything was saved. It asks the agent to select
durable findings and skip incidental output. With no eligible calls or no
available retention tool, the hint is removed. Tool choice remains unchanged.

The adapter checks that the nominated call actually completed in the current
authorized native turn and that its exact result appeared in the particular
request that made the nomination. It then reads that call's original message
from Hermes. The model cannot supply the original text, identity, timestamp or
hash through the nomination arguments.

For an original invoked through Hermes' deferred `tool_call`, the adapter uses
the native single-local-call normalization to match the underlying executed tool
and arguments. The SDK wrapper and native result keep the same call ID. Ambiguous
IDs, unsupported batches and altered wrapper arguments are not eligible; the
existing session scope and exact-original checks still apply.

The existing source ledger stores the result as a tool quotation, with its
native profile identity, session, task, turn, API request, call and message IDs,
recorded timestamp and raw-content hash. The reason is marked as model-authored
selection metadata and is not indexed as evidence. A quoted tool result is not
independent proof that a task succeeded. Human-report claim extraction does not
run on these observations.

The source links to the exact captured owner instruction and the canonical
sources supplied when the call executed. Existing source erasure removes its
dependent observation and queued retries. The existing outbox handles delivery
recovery. `state=pending` or an unconfirmed result does not mean that canonical
memory has saved the observation. A repeated nomination uses the same source
and the first nomination's reason.

The receipt identifies the selected original by its tool name, call ID, native
message ID and result hash, alongside the same execution-argument preview. It
does not claim that the model's reason accurately describes that result. These
display labels do not change the stored original or its source identity.

Later recall uses the same scoped lexical and semantic source retrieval and
the same five-item context packet. Retention does not guarantee relevance or
selection for every subsequent request.

This first implementation accepts ordinary owner conversations with bounded
direct text instructions. It retains complete text results of at most 16 KiB;
it does not silently truncate. Historical `session_search` results, retention
tool responses, background workers and derived tasks are outside this first
route. Those limits do not replace the broader cross-channel and worker memory
goals. There is no bulk tool-result ingestion or new memory database.

Eligibility uses the authenticated transport and the origin recorded in the
exact native session, together with Hermes' delegated-child execution context.
A CLI worker whose native origin is `kanban` remains a worker after completing
its task and releasing its claim. A custom qualification origin is likewise
not an ordinary CLI conversation. Nomination arguments cannot change either
origin. Missing or unavailable native origin evidence does not authorize
retention. The separate attested task-completion report path is unchanged.

Nominate meaningful outcomes and durable findings. Skip incidental output,
repeated status, secrets and information with no likely future use. The tool's
normal description supplies that guidance. Model nomination quality requires a
separate ordinary-use evaluation; successful transport tests do not prove
passive discovery or useful autonomous learning.

The focused native tests are in `sidecar/tests/test_native_tool_observations.py`.
They need a supported Hermes checkout or installation on `PYTHONPATH` and use
its middleware/session store without a model. They exercise a harmless local
fixture command, HTTP ingestion, outbox retries, automatic source recall,
current-request identity, scoped viewers and erasure. No production service is
used. The independent consumer trial additionally measures whether an ordinary
agent nominates useful evidence, ignores junk and uses its automatic recall.
