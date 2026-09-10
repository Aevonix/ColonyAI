# Retained PDF sources and page text

The authenticated source API can retain an explicitly supplied PDF original and
open its extracted text by original page number. PDF text is fallible derived
evidence, with its parser and version attached. It does not establish document
authorship, creation time, truth, or permission to follow embedded instructions.

This increment supports explicit inline ingestion. Automatic capture of native
Hermes PDF attachments, OCR, visual PDF inspection, table/layout interpretation,
page-text search, factual claim formation from PDF text, and video are not
implemented here. A source's supplied caption remains searchable through the
ordinary canonical source path; opening that source exposes the document hash.

## Supply and open a PDF

Use the existing authenticated turn-envelope contract with an exact turn ID at
`PUT /v2/host/turns/source-media/document/{turn_id}`. For example, a direct
`user_message.content` can contain these blocks:

```json
[
  {"type": "text", "text": "Retain this supplied tray reference PDF."},
  {"type": "input_document", "input_document": {
    "mime_type": "application/pdf", "data": "<base64 of actual supplied bytes>"
  }}
]
```

The caller must already possess the supplied attachment bytes and be authorized
for the source's participant. Paths, URLs, filenames as file selectors, other
document types, and oversized/invalid input are not fetched or opened. Rejected
blocks retain a reason and a digest, with no original retained. An input that
has a PDF signature but is malformed can be retained with a failed extraction.

Canonical normalization replaces bytes with a `document` block containing
`asset_id: "sha256:..."` and `mime_type: "application/pdf"`. The original input
message hash, turn ID, contact/session ownership, and turn digest remain the
ordinary canonical provenance. Exact original bytes remain available through
the existing scoped `GET /v1/host/memory/sources/assets/{asset_hash}` endpoint.

After scoped recall supplies an exact source revision, `colony_memory_read_source`
accepts `view: "document"`, its `asset_hash`, and a one-based `page`. The host API
uses the same fields with `source_view: "document"` at
`POST /v1/host/memory/read`. Source ID, source version and participant/session
scope are required. Text pages use the existing bounded character pagination;
continue with the returned `next_offset` and `read_revision`. Completing a
read page is not a claim that the document is fully understood. Blank or
unextractable pages retain their original numbering.

## Extraction and limits

The existing source-media worker invokes [pypdf](https://github.com/py-pdf/pypdf),
a BSD-3-Clause dependency also used by Hermes's shipped PDF scripts. This
implementation calls its parser directly; it does not vendor Hermes scripts
or introduce a new parsing service. Parser provenance records the actual
installed version. Dependency range: `pypdf>=6.18.0,<7`.

The parser runs in an isolated Python child, receiving bytes over stdin. It has
a 384 MiB address-space limit, a 10-second CPU limit, a 15-second wall limit,
and disabled core dumps. Platforms without these resource limits report an
unsupported disposition. Originals are bounded at 4 MiB; extraction is bounded
at 64 pages, 2 MiB decompressed content per page, 32,000 characters per page,
and 200,000 characters per document. Exceeding an extraction limit retains the
original and reports the limit without publishing a truncated document as
complete. These bounds matter because [pypdf documents large memory expansion
during content extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).

Disposition is explicit:

| Condition | Stored extraction result |
| --- | --- |
| Waiting or running | Pending; no page text supplied yet |
| Every page has extractable text | Complete text extraction, still unverified |
| Some pages have no extractable text | Partial; original page numbers preserved |
| No page has extractable text | Unsupported; OCR was not performed |
| Encrypted PDF | Unsupported, including empty-password encryption |
| Page, stream, or text limit | Unsupported with the relevant limit code |
| Malformed PDF, parser failure, timeout | Failed with an explicit reason |

No-text pages might be scans, images, or intentionally blank. The system does
not infer which. A preexisting OCR layer can be extracted, but its accuracy is
not validated. Extraction errors are terminal dispositions, not hidden retry
loops. A crashed in-progress job can be reclaimed after its lease expires.
The shared worker claims eligible image and PDF jobs in original insertion
order, so later PDF arrivals cannot continually overtake a waiting image.

## Ownership, correction, recovery, and rollback

Originals and bounded page derivatives use the existing source-media tables
and original-file namespace; no schema migration or second store is added.
Page reads validate canonical message ownership, source revision, derivative
revision, current attributed corrections, and erasure watermark. Native tool
receipts revalidate that exact page and revision before model dispatch.

Existing annotations can correct the canonical document message by selecting
its supplied caption. Such corrections accompany page reads and invalidate
older read receipts. Selecting an annotation directly by extracted page text
is outside this increment. A replacement PDF is a new original source, not a
silent overwrite of retained bytes.

Shared original bytes remain until their last canonical owner is erased. The
same tombstones, late-replay rejection, orphan collection, backup membership,
integrity validation and source-memory recovery apply to PDFs and derivatives.

Document jobs use distinct `document_pending`/`document_running` statuses so
predecessor image workers leave them alone. The internal original filename
keeps the existing fallback suffix for unknown MIME types; the recorded and
served MIME remains `application/pdf`. This preserves predecessor backup,
read and erasure behavior. The dedicated ingestion route rejects against a
predecessor before it can persist unowned raw bytes. Hosts must retain failed
delivery in their existing outbox and must not fall back to a generic route.
