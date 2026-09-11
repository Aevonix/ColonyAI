# Source forgetting on native Hermes requests

The general adapter uses Hermes `llm_request` middleware to reconcile canonical
source erasure before each normal model request, including resumed sessions and
tool-loop retries. The built-in context compressor remains selected. There is no
custom compressor, model call, background sweep, or new deletion store.

The context response carries the contact's canonical erasure watermark captured
before its producers run. The memory provider stamps its returned packet with
that contact and watermark; source IDs and quotation provenance remain in the
existing evidence body. Hermes wraps and persists this packet as `api_content`.
At request time the adapter consumes the existing erasure feed into the same
durable rules that protect the turn outbox.

The existing pre-turn observation also supplies Hermes' clean content alongside
persisted `api_content`. An ephemeral map for that active turn preserves this
exact association when clock notes or other native context surround the source.
It is cleared on completion and bounded across concurrent turns. Multimodal
messages are matched as complete original block lists after removing appended
packet-only notes, so forgotten image bytes do not survive as residual blocks.
The current direct input is new evidence: an owner can deliberately tell the
agent the same fact again. This exception requires the actual current input
from the authenticated native turn observation and its matching request row;
it never selects an old historical row merely because it is last. Recalled
packets on that new input still undergo freshness checks. Existing canonical
source and outbox tombstones remain unchanged.

- Exact whole-message matches against deleted source hashes become an explicit
  forgotten-source placeholder. Original session and speaker hashes also identify
  exact full-message copies carried into another session for the same contact.
- Retained packets with an older or unknown watermark are removed after erasure.
  This invalidates derived relationship and commitment sections together with
  quotations. Fresh assembled packets remain eligible. Untagged historical
  memory fences are retained only while the contact has no canonical erasures.
- A forget during assembly invalidates that packet on the next freshness check.
  The feed check is the request's observation point; a forget committed after the
  check can only affect the next request. No claim is made to recall an in-flight
  provider request.
- Feed reconciliation has a 250 ms cooperative deadline and at most four pages.
  If it is unavailable or incomplete, earlier conversation history and recalled
  packets are withheld. The current turn and its tool results remain available,
  so an endpoint outage does not replay or silently erase completed build steps.
  Already retained deletion hashes still apply. The middleware returns a reduced
  request instead of raising into Hermes' fail-open middleware behavior.

This closes an exact replay boundary, not all of P3. Native transcript content,
stored `api_content`, inactive compression generations, trajectory files and
backups are not rewritten. A later resume is filtered again. Arbitrary assistant
paraphrases, summaries without retained packet markers, tools containing copied
arguments, static identity files and unlinked historical notes have no invented
lineage. Memory from another contact requires that contact's erasure scope; a
relationship to that contact is not itself source ownership. Storage redaction
needs a supported native row-level operation with real dependency information.

## Native history retrieval

The existing `tool_execution` middleware also reconciles `session_search`
after its ordinary native authority check and before its result reaches the
model or execution observer. This covers discovery, reading, scrolling and
browsing. It uses the selected native database read-only, resolves returned
message IDs to their full bytes, and checks the bounded originating turn's
user/assistant anchors. It does not dump whole sessions or rewrite history.
The same exact source-hash rule used by request replay excludes a forgotten
turn's assistant arguments and tool results, including a scroll window whose
user anchor is outside the returned excerpt. Unrelated turns remain readable.

Optional native message hashes on the existing scoped source-freshness API
resolve to canonical source IDs and versions. The current principal and viewing
session determine visibility; the native session selector supplies no grant.
Known deleted hashes remain resolvable as erased. Authentic returned evidence
uses the existing source-read receipt and supplied-source lineage, so a later
erasure withholds an already-opened result and invalidates canonical descendants.
Quoted marker text never registers a read receipt. Unknown native history has
an explicit untracked count and no fabricated canonical parents.

Native titles and browse previews have no exact source identity. They are
omitted from model-facing history results; session links, time, channel, counts
and traceable message excerpts remain available. Open a session to inspect its
evidence. Unavailable storage, incomplete erasure freshness or an unresolved
native turn returns an explicit failure rather than an empty successful result
or an unchecked fallback.

This is logical non-recollection of known sources, not physical deletion.
Raw native SQLite/FTS history, communication summaries, archived prompts, logs,
backups and previously unlinked paraphrases remain outside this projection.
It does not revoke arbitrary owner shell/file access or make a claim about
bytes already sent to a model. Do not report complete forgetting until the
requested storage surfaces have actually been covered.

The native qualification builds the public wheel, loads it in the pinned Hermes
runtime, calls the real memory provider against canonical source recall, uses
Hermes' own injection and SQLite persistence, then resumes twice through its
built-in compressor and conversation loop. The first captured provider request
contains the neutral source. After the standard forget API, the second contains
neither that exact source nor its stale recall packet. Controlled inference is
used; the test explicitly confirms the native storage limitation remains.
