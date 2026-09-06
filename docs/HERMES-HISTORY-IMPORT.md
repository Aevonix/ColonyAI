# Reviewed Hermes history import

The importer retains selected historical quotations in the existing canonical
source ledger. It does not invoke Hermes, the ordinary turn API, tools, ToM,
affect, commitment extraction or claim learning. The existing lexical index and
semantic source projection queue make those quotations available to the bound
contact across sessions. It does not grant that contact any new authority.

Run it against a **consistent SQLite backup** of Hermes `state.db`, never an
actively written database. SQLite's backup API can produce that copy while
Hermes stays online. A nonempty companion WAL is rejected. The source is opened
read-only and its exact file digest identifies each resumable import run.
The command has no model client, attachment fetcher or network transport.
An already running semantic worker can index the retained text afterward.

## Bind people before copying messages

Hermes stores routing origin on the session, not an authenticated actor on every
historical message. A `user` role alone cannot establish that the owner said
something. A cron instruction or a sub-agent task may also use that role.

Prepare a private mapping with a stable namespace for this Hermes database and
an explicit binding for every selected session:

```json
{
  "version": 1,
  "namespace": "my-hermes-home",
  "reviewed": false,
  "bindings": [
    {
      "session_id": "historical-direct-session-id",
      "platform": "sms",
      "actor_id": "verified-transport-actor",
      "chat_id": "verified-direct-chat",
      "chat_type": "dm",
      "contact_id": "existing-colony-contact-id",
      "review_evidence": {
        "kind": "operator_review",
        "reference": "private verified transport binding"
      }
    }
  ]
}
```

Current verified contact handles or an installed private phone authority binding
can support the review. An inferred contact, a matching display name or a
speaker's assertion of authority cannot. The importer checks the exact recorded
session platform, actor, chat and DM type, plus agreement with populated
`origin_json` fields. It does not infer missing identities from compression
parents, fill absent chat fields or identify individual group speakers. It
rejects explicit automation sources even if a mapping claims they are DMs.

Unmapped sessions, groups, automation and unresolved identities remain in the
original Hermes database. Dry-run reports their counts by source. They are not
copied under a fabricated owner/contact or automatically made searchable across
private channels. This first increment deliberately leaves some useful older
history unavailable until its attribution can be established.

## Preview, review and apply a finite batch

```sh
python -m colony_sidecar.turns.hermes_history \
  --database /private/hermes-history-backup.db \
  --mapping /private/history-mapping.json --dry-run
```

Dry-run reads all messages in the explicitly bound sessions, writes no target
database and makes no model calls. It counts eligible source quotations and
each exclusion. After reviewing the mapping and counts, change `reviewed` to
`true`. The file is an operator input, not evidence that approval can be inferred
from a conversation. Apply to the selected instance's explicit state directory:

```sh
python -m colony_sidecar.turns.hermes_history \
  --database /private/hermes-history-backup.db \
  --mapping /private/history-mapping.json \
  --state-dir /private/colony-instance --apply --limit 1000
```

Repeat the same command while `remaining` is nonzero. The limit counts scanned
messages, including exclusions. One small progress table lives in the same
canonical ledger. The source commits before its cursor is acknowledged; an
interruption between those operations safely repeats the idempotent source write.
Concurrent importers cannot move the same cursor backwards. A changed snapshot
or mapping starts a new scan, while the stable database namespace/message IDs
keep already retained sources idempotent. Conflicting content or attribution for
an existing source ID stops the import instead of silently replacing it. Keep
the namespace stable across backups; use a different namespace for another
Hermes installation.

## Evidence and erasure semantics

Each imported user or non-tool assistant message remains a quotation with its
original role, content, session ID and message hash. Provenance records the
Hermes row ID, platform, actor, chat, review evidence and platform message ID when
available. The timestamp is explicitly a **Hermes-recorded timestamp**, not proof
of when the described event happened. A session's model name is retained only as
a session hint; per-message model and weight revision remain unknown. No system
prompt, model configuration, reasoning trace, tool arguments or tool result is
copied. Source provenance is retained in `turn_sources.messages_json`; recall
uses the existing source ID and role, with the same shared selector and budget.

Compacted original messages are retained. Rewound/inactive messages, generated
summary markers, ambient observations, tool-call messages and special display or
effect records are excluded. Text-only block lists remain structured. Historical
attachments stay in Hermes: the importer neither follows their URLs nor runs old
vision jobs. This is a text-source increment, not historical media recovery.

Existing canonical source tombstones block the same original session/message
from being reimported under a history ID. Erasing an imported source blocks later
retry and removes its canonical quotation and linked search projections through
the normal erasure path. Historical assistant utterances are separate evidence;
the importer does not invent causal lineage between a prior user assertion and
every later paraphrase. Unlinked legacy graph summaries and the original Hermes
backup are outside canonical source erasure. A request to forget a fact from all
history therefore still needs the appropriate source set and the deployment's
backup/Hermes erasure procedure; this importer does not claim retroactive global
forgetting.

`test_hermes_history_import.py` exercises dry-run isolation, interrupted resume,
source-only queues, exact binding rejection, cross-session scoped HTTP recall,
structured text provenance and both ordinary-source and imported-source erasure
fences with real SQLite. These tests establish the import path. An actual
deployment import additionally requires reviewing its private mapping and then
observing a retained historical quotation through its production context path.
