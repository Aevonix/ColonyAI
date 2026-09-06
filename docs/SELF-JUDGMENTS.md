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
