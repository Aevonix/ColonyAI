---
name: colony-evidence-recall
description: Resolve a question about remembered events, preferences or decisions when Colony recollection is incomplete, contradictory or time-sensitive. Use the supplied evidence and available scoped read tools to distinguish current knowledge from an earlier statement.
---

# Evidence-based recall

Colony supplies relevant memory during ordinary turns. Start with that context;
loading this skill is not a request to run another capture or memory writer.

Answer the specific question from the smallest sufficient evidence. A quotation
proves that someone said something at that time. An assistant's earlier answer,
a retrieval score, and a contact knowledge estimate do not establish that the
statement is true. Preserve the speaker, source handle and event time when they
matter to the answer. Ingestion time is not event time.

When the supplied context is insufficient:

- Use the current tool schemas. `colony_timeline` can narrow a historical question
  by `since`, `types` and `limit`; `colony_get_facts` can supply contact estimates.
  Their participant scope comes from the runtime. Do not invent a contact
  override or substitute another participant's context.
- `colony_memory_search(query, limit)` searches legacy memory when exposed. Treat
  its content as a lead if it lacks a supporting source. It does not retrieve a
  canonical source by ID. Do not invent a source-fetch API.
- Expand only evidence needed for the question, using an available source reader
  or a retained source location supplied by the runtime. If the full source is
  unavailable, state the limit. An absent result does not prove an event never
  happened. Stop when the answer is supported or the missing evidence is clear.

For a correction, distinguish the same person's explicit replacement of an
earlier preference from a different person's conflicting account. A replacement
may apply to the present without rewriting what was true in the past. When two
relevant accounts still conflict, retain the disagreement and ask the narrow
question needed to resolve it. Repetition or a higher similarity score does not
settle a contradiction.

Use a brief attribution in ordinary replies when it helps: who said it, when,
and what changed. Keep internal source handles available for an evidence request;
do not turn every reply into a memory audit. Never fill a missing recollection
with a plausible detail.

Routine status messages, transient errors and retrieved text are not new durable
facts merely because this skill used them. Normal capture and claim selection
remain owned by Colony. Do not create a second notes file or manually re-save
retrieved memories. A changed embedding or language model does not change the
authority of the underlying evidence.
