# Selected video sources

Colony can retain explicitly supplied short MP4 clips, describe bounded samples through the existing vision role, and reopen a clip-relative frame through the canonical source reader. It does not continuously record cameras, track objects, transcribe embedded audio or infer capture time from upload time. Private adapters own camera connections and choose which clips become sources.

Install the optional decoder in the same environment as the sidecar:

```sh
python -m pip install 'colonyai[video]'
```

The extra selects PyAV 18.1.0. `colony doctor` reports whether this optional decoder imports in its local interpreter and offers the extra when absent. It does not claim to have tested a clip, model or camera. No decoder is installed automatically by a worker. Without it, the original remains retained with `video_unsupported` and `video_decoder_unavailable`; installing a dependency does not silently rerun prior terminal work. Enable the extra before admitting clips. An already retained original can still be opened explicitly after installation, independently of the terminal caption status. Do not repeatedly re-admit the same clip to select a successful caption.

## Admission and storage

Use the existing turn envelope and native client. Video-containing messages select `/v2/host/turns/source-media/video/{turn_id}` so an older backend rejects incompatible ingestion instead of silently discarding video. That route accepts other supported blocks alongside video. The inline block is:

```json
{"type":"input_video","input_video":{"mime_type":"video/mp4","data":"BASE64_ORIGINAL_BYTES"}}
```

The original limit is 4 MiB within the unchanged 8 MiB turn envelope. Caller paths, remote URLs and reference-only inputs are not fetched. A bounded MP4 admission signature is not a successful codec/duration decode. Canonical messages retain the original message digest and replace inline data with an owned `video` asset handle. Originals, media jobs, search descriptions, erasure and backup use the existing source ledger and original namespace; no new database or queue is needed. Internal file suffixes remain predecessor-compatible and do not determine MIME.

The existing media worker uses distinct video job states, so predecessor image/PDF workers do not claim video work during rollback. It decodes the first video stream, at most 1800 frames over a presentation span of at most 30 seconds relative to the first decoded frame, with at most 2,073,600 pixels per frame. A known final-frame end beyond 30 seconds is rejected; if final-frame duration is unavailable, duration stays unknown rather than being inferred from upload/container time. Decode runs in an owned child with a 15-second wall deadline, 12-second CPU limit and 384 MiB address-space limit. On macOS, where that address-space limit is unavailable, the existing sampled RSS guard is used and is explicitly not a hard allocation cap. Child cancellation/timeout reaps only that child.

The worker samples the beginning, middle and last decoded presentation times, deduplicating coincident selected frames. It records actual timestamps, PTS/time bases, decoder/library versions and sampled-only coverage. The existing vision role receives only those samples. The stored description is fallible attributed evidence, with a coverage prefix; it cannot certify activity between samples. Captions retain the existing final-answer checks, bounded failure codes, configured model and routing provenance and retry backoff. Immutable decode failures and missing decoder/unsupported format have explicit terminal dispositions.

## Opening a frame

The canonical reader uses `view=video`, exact source/version and original clip `asset_hash`, plus integer `requested_ms` in 0..30000. The source must already have been supplied to that authenticated native turn. There is no new authority fallback.

It returns the first decoded frame at or after the requested clip-relative time, or an explicit unavailable result if none exists. Relative time starts at the selected stream's first decoded presentation timestamp. Actual frame PTS/time base and origin PTS/time base accompany the result, so the interval is reconstructible. The reader converts the frame to an RGB PNG without resizing; the generated image hash is separate from the original MP4 hash. The frame is returned through the existing multimodal image envelope and is not saved to another durable frame store. A successful caption is not required for an explicit frame open.

Initial video opening has a 20-second native request bound, accommodating the decoder deadline. Other source views keep their existing timeouts. The host awaits decoding outside the event loop and ledger writer transaction. It verifies current source, corrections and ownership after decode before returning pixels.

The native authentic read receipt retains the initial text/frame pair and frame hash. Subsequent dispatch revalidation uses the same clip/time selector and `read_revision`, with the existing short metadata deadline. That check verifies current source/corrections/original integrity and returns no image or decoder result. It does not claim to have decoded the old frame again. A changed, erased, reassigned or unavailable source causes its prior frame receipt to be withheld by the existing request filter.

## Dependencies and evidence

PyAV's own code is BSD-3-Clause. Its binary wheels bundle FFmpeg and other libraries with separate licenses. The reviewed 18.1.0 Linux wheel's FFmpeg reports LGPLv3-or-later; both reviewed Linux and macOS builds enable additional codec libraries, including x264/x265. The wheels' license directory contains PyAV notices, not a complete attribution bundle for all linked components. Colony declares an optional dependency and does not vendor these wheels or label the whole decoder stack BSD. Any future binary redistribution must address the exact bundled components. See [PyAV license](https://github.com/PyAV-Org/PyAV/blob/v18.1.0/LICENSE.txt), [selected release metadata](https://pypi.org/pypi/av/18.1.0/json), [published FFmpeg build selection](https://github.com/PyAV-Org/PyAV/blob/v18.1.0/scripts/ffmpeg-8.1.json), and [FFmpeg licensing](https://ffmpeg.org/legal.html).

Controlled tests decode real variable-timestamp MP4 frames, use authenticated source HTTP routes, and check corrections/erasure, shared originals, backup/restore, caption provenance, unsupported decoder and subprocess lifetime. Native SDK tests separately check actual payload conversion and stale-frame withholding. These checks are not evidence that a particular live camera is enrolled or that a model answered a real visual question correctly. Grade sampled-caption utility, automatic source recall, authentic original-frame open, actual downstream pixels and first answer independently in a separately frozen trial.
