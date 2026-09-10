# Durable source images and recalled descriptions

Native Hermes images become local originals, fallible descriptions, and scoped recall candidates across sessions. The existing source reader can reopen an explicitly selected original into supported vision tool-result parts. Description recall and original-pixel inspection are distinct operations. Video timelines, image embeddings and automatic selection of a vision processor remain separate work.

## The source contract

Hermes v0.21 sends native images as OpenAI content lists. The general adapter now retains those lists in the ordinary durable outbox and sends them through `TurnSyncRequest`; it no longer converts them to a Python string. Existing text cognition receives only explicit text blocks. Checkpoints already retain content lists and use the same image storage path.

Automatic retention accepts inline `image_url` or `input_image` data URLs containing a static PNG, JPEG or WebP, at most 4 MiB per image and 32 million pixels. The enclosing source/outbox envelope remains limited to 8 MiB. No URL is fetched and no local path from message text is opened. The transport must supply the bytes. Unsupported attachments become explicit `image_unretained` records. Remote image references retain a digest and reason instead of signed URL credentials. This normalization does not redact a URL independently written in ordinary source text.

The source message stores a content-addressed asset handle. Its original message digest is retained as ledger-owned `_source_message_hash`, so source erasure and host outbox/checkpoint copies still agree. Public message schemas cannot supply that top-level field. The immutable turn digest continues to cover the original input envelope. Current message content and source membership still govern access; a hash is not an authorization grant.

An already captured source is not silently reinterpreted or backfilled. Rolling-upgrade source replay retains its previous digest contract. Existing historical references and unsupported attachment kinds keep their documented limitations.

## Storage and lineage

The existing `LocalImageStore` provides exact-byte storage in the source-owned namespace:

```
$COLONY_STATE_DIR/images/sources/originals/<sha256>.<extension>
$COLONY_STATE_DIR/images/sources/thumbs/<sha256>.jpg
```

Originals are not resized or EXIF-stripped. Thumbnails are separate projections. Files are written with private permissions, an atomic replacement and durability sync before the source transaction references them. A read verifies the original hash. The source namespace keeps cleanup independent from existing vector-store image ownership.

`source_media` and `source_media_links` use the existing canonical turn SQLite ledger. Links identify the source turn, original message hash, block index and role. Descriptions keep the producing model alias and description version. They are `derived_unverified` evidence, not source quotations or verified beliefs.

Descriptions share the existing source projection worker, with one model request at a time. The explicitly configured local VISION role must declare image support. No SMALL/default binding is borrowed. The call has a 20-second timeout and router escalation is disabled. Unavailable or incompatible local models leave visible pending work. Model aliases are recorded without claiming an immutable weights revision. `COLONY_SOURCE_CLAIMS=off` stops the shared source projection worker; explicit erasure still attempts physical media cleanup.

Configure the named role through the existing host LLM configuration, for example:

```json
{"provider":"local","baseUrl":"http://127.0.0.1:8080/v1","models":{"vision":{"model":"local-vision-model","supportsVision":true}}}
```

`supports_vision` is also accepted. The declaration must follow an actual image qualification of that endpoint/model; it is not inferred from the name. Normal text scoring and escalation never select the VISION role.

A source deletion atomically removes its image links. If no surviving source owns the pixels, the description and search projection are removed and the original/thumbnail become cleanup work. Successful deletion removes those files; failure reports `media_cleanup: pending`, and later worker passes or an explicit retry attempt cleanup again. Shared pixels remain only for surviving sources, and the deleted contact cannot read or recall another contact's copy. Late caption results check lease ownership and surviving source links before committing.

Startup recovers files stranded before a source transaction committed, only in the source-owned namespace and under the ingest write lock. Backups of the ledger must include the image namespace; restoring the database alone cannot recreate original pixels. This increment does not erase external transport caches or old backups, and cannot retract evidence already sent in an earlier response.

## Recall and inspection

Descriptions have a rebuildable local FTS index. Authorized matches enter the same candidate selection, reranking and character budget as textual source evidence and graph memories. There is no second media injection. The packet carries the asset handle, source turn, role, model alias and uncertainty label. Description recall remains available after changing the interaction model because descriptions and originals live outside the model.

The source status endpoint now includes recent media jobs:

```
GET /v1/host/memory/sources/claims/status?contact_id=...
```

Original bytes are available only through the authenticated, contact-scoped API, with source/session ownership checked and caching disabled:

```
GET /v1/host/memory/sources/assets/<sha256>?contact_id=...&session_id=...
```

There is no public static file route. The existing `colony_memory_read_source`
tool accepts `view: "image"` with a supplied `source_id`, `source_version`, and
`asset_hash` (the 64 hexadecimal characters after `sha256:`). The source
revision must already have reached this participant's current model request.
The tool accepts no file path, URL, participant override or image service.
Opening another person's source or an asset not in that exact source fails.

The tool uses the existing scoped `POST /v1/host/memory/read` route with
`source_view: "image"`. It returns original pixels together with their source
message role, recorded/reported times, exact source references and attributed
corrections applying to the image's owning messages. Interpretations remain
unverified. Corrections are not truncated to make an image fit: a correction
bundle over 16,384 characters returns unavailable without sending pixels;
the textual source reader can still page its evidence. Original static-image ingest limits still apply. Audio
and unsupported attachment types cannot enter this image view.

Hermes receives its supported `_multimodal` tool envelope: one provenance text
part and one `image_url` part containing the original data URL. The text does
not duplicate base64 image data. Hermes applies the active processor's vision
and tool-result capability rules. If it selects the text fallback, the result
explicitly says no visual inspection occurred and `image_bytes_included` is
false. Colony does not select a new model or silently recaption the original.

Qualify the model identifier on the constructed native request, including any
explicit voice or CLI override. A named provider may resolve its default model
correctly while a caller passes the provider alias as the model. If the vision
declaration names only the concrete model, Hermes can then choose the text
fallback even though the endpoint accepts the alias. Align the selected role
model and its capability declaration through the deployment's existing publisher;
a successful description call alone does not verify this native boundary.

Before each actual model dispatch containing an authenticated image read,
Colony verifies the exact source, ownership, asset and read revision again.
That metadata-only call shares the existing 250 ms request freshness budget
and never downloads the pixels again. A new applicable correction changes the
read revision even without erasure. Unavailability, changed ownership,
erasure, missing original, changed correction set or altered tool parts
withholds that result. An explicit fresh opening is then needed. Read receipts
expire with the turn; historical image tool results cannot replay under a new
turn's authority. Filtering copies the outbound request without rewriting the
native transcript. Evidence already dispatched cannot be retracted.

Chat tool parts, Responses `input_image` output parts and Anthropic base64
image tool-result parts are qualified against Hermes v0.21.1. Other native
protocols require their own qualification. A data URL retained in a transcript
or a recalled description alone is not evidence that a vision model inspected
the original.

## Qualification

The source tests exercise actual image files, source API ingestion, scope isolation, durable reopening, canonical hash preservation, checkpoint/outbox erasure agreement, late-description rejection and cleanup of originals/thumbnails. The native packaged adapter test uses Hermes' actual `build_native_content_parts` and lifecycle hooks, then checks the durable outbox and client serializer preserve the content list.

The packaged source-reader test uses installed Colony tools, the real scoped
source API, actual native tool dispatch and Hermes' Chat, Responses and
Anthropic conversion paths. It verifies exact original bytes in supported
image parts, honest nonvision/unsupported-adapter fallbacks, and erasure before
the next dispatch. Focused tests also cover correction/ownership changes,
outage, altered output parts, oversized originals/corrections and erasure
racing a file read. Provider capability declarations are controlled and no
inference calls are made. This establishes protocol and lifecycle behavior,
not visual accuracy or useful ordinary-conversation recollection.

One private neutral image was also described through an explicitly configured local VISION role in 2.47 seconds. The real source API, caption worker and context route correctly recalled the description in another session, withheld it from another contact, reopened the exact original bytes and erased the original/description. This is one working image loop, not a vision accuracy benchmark or production acceptance. Deployment still needs a real channel attachment observed through the deployed adapter and memory provider, followed by a useful visual question whose answer requires the original pixels. Future image embeddings should preserve the same source ownership and erasure lineage.
