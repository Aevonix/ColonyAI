# Attributed corrections to retained source evidence

`POST /v1/host/memory/sources/annotations` appends a correction to an exact source
revision without rewriting the original. It requires authenticated `memory:write`
authority and the same person/session source visibility as ordinary recall. The
server records the authenticated principal and recording time. Request text cannot
claim another author or turn an ordinary message into an authorized correction.

```json
{
  "contact_id": "example-person",
  "session_id": "later-review",
  "annotation_id": "review-1",
  "source_id": "example-report",
  "source_version": "<64 lowercase hexadecimal characters>",
  "excerpt": "Verification occurred at 09:14.",
  "correction": "The digest comparison is supported, but its verification time was not measured. The reported time is unsupported, not disproven."
}
```

The source version comes from structured source references returned by recall.
The excerpt must occur exactly in a retained text message. Excerpt and correction
are each limited to 4,096 characters. An idempotency key is scoped to the person
and authenticated principal; exact replay returns `created:false`, while changed
evidence under the same key conflicts. The response returns the annotation's
`source_id`, `source_version` and exact `target` reference.

The annotation is a new assistant evidence source in the existing source ledger,
with a relation to its target committed in the same SQLite transaction. It does
not create a USER assertion or assert that the correction is independently
verified. Existing source dependencies link the annotation to the target.

Lexical and semantic candidates, source-backed belief/assertion bundles, and
answers with recorded supplied-source ancestry receive the applicable corrections
before ranking. The original evidence and full attributed corrections form one
atomic packet. If it cannot fit the configured context budget, that packet is
omitted. Different original excerpts remain distinct; an annotation-only search
hit does not repeat an already supplied correction bundle. Multiple corrections
remain visible without treating the newest one as automatically true.

Selected packets are checked again after ranking for changed source revisions or
annotations. Native context receives the existing complete section body and exact
source references for both original and correction. No native adapter protocol
change is needed. Newly captured answers therefore retain both dependencies.

Forgetting either source invalidates dependent answers through existing erasure
rules. Erased annotation text is removed; the content-free relation remains so an
unqualified original does not silently return. Partial source erasure never
rebinds the correction to another revision, and unrelated surviving message
evidence remains available. History without supplied-source lineage cannot be
inferred to depend on an annotated source.

This is an attributed source correction, not an automatic fact verifier, topic
matcher or model judge. Text outside canonical source evidence and non-text
messages cannot be annotated through this route. Older backends retain the
additive rows but do not implement mandatory correction expansion; that guarantee
requires the annotation-aware backend. Existing native clients remain compatible.
