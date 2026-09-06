# Revisable working judgments

An ordinary attributed owner turn can now produce a durable agent judgment:
a topic, stance, reason, supporting and contrary source references, stated
certainty, predecessor revision and actual model/configuration provenance.
The existing source worker performs this reflection using the configured
`reasoning` role. It owns one in-flight reflection while continuing source
indexing, claim extraction and image captioning; no additional service is needed.

The model is asked to abstain on transient logistics, copied preferences,
unsupported generalizations and views without lasting use. This is an inference
requirement, not proof that every accepted judgment is good. Output validation
requires a completed final answer and at least one retained current-source
reference. A later reflection receives bounded quotations rehydrated from prior
canonical evidence as well as the prior model's explicitly fallible view.

Each normalized topic can change once per day by default. Operators can set
`COLONY_SELF_JUDGMENT_INTERVAL_SECONDS` to another nonnegative interval. Contrary
evidence received during that interval stays eligible for reconsideration when
it ends. Replaying the same source does not create another vote. Unavailable
inference receives at most three attempts; source bytes, the captured topic head
and the current processing lease are checked again inside the commit transaction.

The current `/v1/host/self` perspective exposes `judgments` and
`judgment_history`. `applies_to=owner_turn_deliberation` describes the implemented
effect: up to two relevant judgments, within 2,400 characters, enter the existing
owner-only working-perspective section during ordinary context assembly. An
unrelated or empty query adds no judgment text. These views do not change owner
preferences, memory truth, tool authority or initiative priority. Existing
automatic legacy opinion weights remain non-governing.

The existing `POST /v1/host/learning/correction` accepts an exact current
`judgment_id`, a stable `correction_id`, and `judgment_action` of `withdraw` or
`reconsider`, alongside its ordinary identity/context/correction fields. It uses
the existing `memory:write` scope and resolves the configured owner. Withdrawal
immediately removes the view from current context and appends an owner control
record; later automatic output for the same normalized topic remains withheld.
This does not promise semantic suppression under arbitrary topic renaming.

Reconsideration requires a retained, attributed owner `source_id`. It keeps the
view withdrawn while the existing worker reasons from that evidence, permits
one reconsideration assignment without the ordinary daily wait, and requires
the same topic. An abstention leaves withdrawal in place. No owner wording is
installed as an agent stance. Repeating a correction ID is idempotent; a stale
target is rejected, and changing the head fences any in-flight reflection.
Owner control records remain visible in judgment history. Source erasure clears
dependent correction text too while preserving value-free withdrawal records.
The latest ten processing records are exposed under `judgment_processing`,
including fixed local validation codes without raw provider responses.

The native `colony_judgments` tool exposes inspection and these controls during
an ordinary owner conversation. It returns the latest ten views/control records
with truncation flags; its model arguments contain only the operation, exact
judgment ID and (for reconsideration) retained source ID. Identity and the
correction instruction come from the transport-bound owner turn. The instruction
is retained as an excerpt of up to 1,500 characters. A current turn's source can
be selected after normal source capture; the tool does not fabricate a source
to satisfy reconsideration. Scheduled, subagent and internal review turns cannot
perform owner controls. A built-wheel native fixture exercises actual owner
conversation tool dispatch over HTTP, guest/scheduled denial and subsequent
reflection using controlled inference, without adding canonical sources.

The projection and processing records use the canonical source SQLite database.
Only new attributed owner sources eligible for ordinary claim derivation enqueue
reflection; historical imports and session-scoped transcript checkpoints do not.
Source erasure removes dependent judgment prose and topic text, including
superseded history. Opaque head tombstones prevent an older view from reviving.
Fresh retained evidence can establish a new view later.

Focused tests exercise scoped HTTP ingress/context, two controlled processor
identities, changed views with contrary evidence, persistence, erasure, concurrent
heads and lease recovery. They demonstrate the storage and behavior contract;
they do not establish semantic quality or coherence across actual live models.

A bounded neutral LAN exercise ran the production projection against two
configured models. Both formed and revised a checkpointing view and abstained
on routine logistics. One model introduced an unsupported duration and count;
the original results were retained. After making quotation grounding explicit
and replacing semantic fixture IDs with opaque IDs, that model's three cases
were repeated once. Its initial explanation stayed within the reported counts
and outcomes, but the revision failed validation and logistics again produced
abstention. The failed revision retained processor metadata and its error class,
not the raw output or exact validation reason. No general quality pass follows
from this exercise, and no production owner facts or judgments were created.
