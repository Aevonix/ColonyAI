# Durable source images and recalled descriptions

This increment closes one image memory loop: a native Hermes image becomes a local original, a fallible description, and a scoped recall candidate across sessions. It does not implement audio transcription, video timelines, image embeddings or automatic visual reinspection.

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

There is no public static file route. A model-facing visual reinspection tool is a subsequent increment; an asset handle or caption alone is not equivalent to showing the model those pixels again.

## Qualification

The source tests exercise actual image files, source API ingestion, scope isolation, durable reopening, canonical hash preservation, checkpoint/outbox erasure agreement, late-description rejection and cleanup of originals/thumbnails. The native packaged adapter test uses Hermes' actual `build_native_content_parts` and lifecycle hooks, then checks the durable outbox and client serializer preserve the content list.

One private neutral image was also described through an explicitly configured local VISION role in 2.47 seconds. The real source API, caption worker and context route correctly recalled the description in another session, withheld it from another contact, reopened the exact original bytes and erased the original/description. This is one working image loop, not a vision accuracy benchmark or production acceptance. Deployment still needs a real channel attachment observed through the deployed adapter and memory provider. Future image embeddings and visual reinspection should complement these descriptions, preserving the same source ownership and erasure lineage.
