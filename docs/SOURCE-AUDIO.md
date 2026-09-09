# Original audio and timed transcripts

The canonical source ledger accepts bounded, already supplied PCM WAV clips.
It retains the exact original in the existing source-original namespace and
links it to the attributed source message. This uses the same scoped asset
read, source revisions, outbox, erasure, orphan cleanup and backup paths as
images. No remote audio URL is fetched and no new speech model is invoked.

The initial format is mono 16-bit PCM WAV, 8000 to 48000 Hz, at most 60 seconds and
4 MiB, within the existing turn envelope limit. `input_audio` uses
`{"format":"wav","data":"<base64 bytes>"}`. An immediately following
`audio_transcript` block can carry an exact audio hash, bounded segments with
clip-relative `start_ms`/`end_ms` and text, recognizer model/revision,
`received_at`, and optional `captured_at`. The latter stays null when unknown;
receipt time minus duration is never presented as capture time. A whole-clip
segment does not imply word alignment, verified speech or known confidence.

Normalization removes inline bytes from source JSON, replacing them with the
asset hash, MIME, decoded duration/sample metadata and a labelled unverified
transcript. Original input hashes survive normalization, so a delayed direct
reply can still resolve its exact captured parent. Transcript wording belongs
to its source message, not a shared mutable caption for every owner of a clip.

Lexical and semantic recall provide the machine-transcript text and opening
handle with `derived_unverified` state. Text-only models receive no raw audio
block. The existing memory source reader opens current metadata with its
revision/cursor, and `/v1/host/memory/sources/assets/{sha256}` serves original
bytes only to the currently authorized participant/session. No static file
route or filesystem path is injected into the model.

Multimodal source messages continue to bypass direct-text assertion extraction.
They remain source evidence for recall; this packet does not treat a machine
transcript as independently verified user speech. A deployment should qualify
derived speech formation before enabling this capture for every turn that
previously produced ordinary text claims. It can select original-audio capture
for a particular surface while retaining the ordinary text path elsewhere.

The existing turn API has a `source-media/audio` variant. Updated clients use
it for audio source payloads; predecessors reject its path/body ID mismatch
before storing unsupported raw bytes. Upgrade the sidecar and client before
the capturing adapter. Unsupported encodings, remote audio, video and invalid
transcript pairings are explicitly marked unretained. They never appear as a
successfully owned audio asset. Transcription or decoding of unsupported media
is not attempted.

Forgetting removes the source and dependent replies, fences stale reads and
vector projections, and deletes the original once no current owner remains.
The same original can remain available to an independently authorized owner.
Offline delivery consumes canonical erasure rules before replay. Full backups
and memory-only salvage preserve owned WAVs through the existing original-file
checks; their legacy `source_images` counters also count audio assets.

This does not import historical full-call recordings, perform continuous
capture, retain video clips, identify speakers from sound, infer affect or
retract audio already played. A new device path needs its own admitted source
event and transport witness before it can claim these guarantees.
