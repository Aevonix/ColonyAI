"""Selected MP4 originals and bounded frame decoding in an owned child.

No caller paths, network inputs or audio processing. Imports remain standard
library until the child has installed resource limits.
"""
from __future__ import annotations

import asyncio
import base64
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import sys

MAX_VIDEO_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_DURATION_MS = 30000
MAX_PIXELS = 1920 * 1080
MAX_FRAMES = 1800
MAX_DECODE_SECONDS = 15
MAX_OUTPUT_BYTES = 3 * ((MAX_FRAME_BYTES + 2) // 3) * 4 + 32768
VERSION = 'source-video-samples-v1'


def decode_video_input(block):
    item = block.get('input_video')
    if (set(block) != {'type', 'input_video'} or not isinstance(item, dict)
            or set(item) != {'mime_type', 'data'} or item.get('mime_type') != 'video/mp4'
            or not isinstance(item.get('data'), str)):
        raise ValueError('unsupported_video_input')
    if len(item['data']) > ((MAX_VIDEO_BYTES + 2) // 3) * 4:
        raise ValueError('video_bytes_exceed_limit')
    try:
        data = base64.b64decode(item['data'], validate=True)
    except (ValueError, TypeError):
        raise ValueError('invalid_video_encoding') from None
    if len(data) > MAX_VIDEO_BYTES:
        raise ValueError('video_bytes_exceed_limit')
    # Admission only, not a duration/codec/decode claim. No arbitrary file fetch.
    if len(data) < 16 or data[4:8] != b'ftyp':
        raise ValueError('unsupported_video_format')
    return data


def disposition(status, reason=None, **metadata):
    return {'version': VERSION, 'status': status, 'reason': reason,
            'audio_processed': False, 'coverage': 'sampled_frames_only', **metadata}


def _decode(data, requested_ms):
    try:
        import av
    except ImportError:
        return disposition('unsupported', 'video_decoder_unavailable')
    metadata = {'decoder': 'PyAV', 'decoder_version': av.__version__,
                'libraries': {key: list(value) for key, value in av.library_versions.items()}}
    try:
        av.logging.set_level(av.logging.PANIC)
        if len(data) > MAX_VIDEO_BYTES:
            return disposition('failed', 'video_bytes_exceed_limit', **metadata)
        with av.open(io.BytesIO(data), format='mov', options={'protocol_whitelist': 'pipe'}) as container:
            if not container.streams.video:
                return disposition('unsupported', 'video_stream_absent', **metadata)
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            if not 0 < stream.width * stream.height <= MAX_PIXELS:
                return disposition('unsupported', 'video_dimensions_exceed_limit', **metadata)
            records, origin, previous = [], None, None
            # Store only bounded samples, not the full decoded clip. For worker
            # sampling keep first, middle candidate and last via a second pass
            # after actual decoded timestamps establish the clip's span.
            for index, frame in enumerate(container.decode(stream)):
                if index >= MAX_FRAMES:
                    return disposition('unsupported', 'video_frame_count_exceeds_limit', **metadata)
                if frame.pts is None or frame.time_base is None or frame.time_base <= 0:
                    return disposition('unsupported', 'video_frame_timestamp_unavailable', **metadata)
                timestamp = frame.pts * frame.time_base
                if origin is None:
                    origin = timestamp
                if previous is not None and timestamp <= previous:
                    return disposition('unsupported', 'video_nonincreasing_timestamps', **metadata)
                previous = timestamp
                relative = (timestamp - origin) * 1000
                if relative > MAX_DURATION_MS or not 0 < frame.width * frame.height <= MAX_PIXELS:
                    return disposition('unsupported', 'video_decode_bounds_exceeded', **metadata)
                duration = getattr(frame, 'duration', 0) or 0
                records.append((frame.pts, frame.time_base, relative, duration))
            if not records:
                return disposition('failed', 'video_frames_absent', **metadata)
            last = records[-1]
            duration_ms = last[2] + last[3] * last[1] * 1000 if last[3] > 0 else None
            if duration_ms is not None and duration_ms > MAX_DURATION_MS:
                return disposition('unsupported', 'video_duration_exceeds_limit', **metadata)
            targets = ([Fraction(requested_ms)] if requested_ms is not None
                       else [Fraction(0), last[2] / 2, last[2]])
            selections = []
            for target in targets:
                selected = next((i for i, row in enumerate(records) if row[2] >= target), None)
                if selected is None:
                    return disposition('failed', 'video_frame_at_time_unavailable', **metadata)
                if not any(index == selected for index, _ in selections):
                    selections.append((selected, target))
            # A fresh bytes-only decoder avoids approximate keyframe seeking.
            frames = []
            with av.open(io.BytesIO(data), format='mov', options={'protocol_whitelist': 'pipe'}) as pixels:
                selected_stream = pixels.streams.video[0]
                selected_stream.codec_context.thread_count = 1
                for index, frame in enumerate(pixels.decode(selected_stream)):
                    for chosen, target in selections:
                        if index != chosen:
                            continue
                        pts, time_base, actual, _ = records[index]
                        if frame.pts != pts or frame.time_base != time_base:
                            raise ValueError('unstable_video_decode')
                        image = frame.to_image()
                        output = io.BytesIO(); image.save(output, format='PNG')
                        raw = output.getvalue()
                        if len(raw) > MAX_FRAME_BYTES:
                            return disposition('unsupported', 'video_frame_bytes_exceed_limit', **metadata)
                        frames.append({'requested_ms': float(target), 'actual_ms': float(actual),
                            'frame_pts': pts, 'time_base': f'{time_base.numerator}/{time_base.denominator}', 'stream_index': stream.index,
                            'origin_pts': records[0][0],
                            'origin_time_base': f'{records[0][1].numerator}/{records[0][1].denominator}',
                            'selection': 'first_frame_at_or_after', 'timestamp_origin': 'first_decoded_frame',
                            'source_width': frame.width, 'source_height': frame.height,
                            'width': image.width, 'height': image.height, 'transform': 'rgb24_png_no_resize',
                            'asset_hash': hashlib.sha256(raw).hexdigest(), 'mime_type': 'image/png',
                            'data_url': 'data:image/png;base64,' + base64.b64encode(raw).decode()})
                    if index >= max(i for i, _ in selections):
                        break
            return disposition('complete', **metadata,
                duration_ms=float(duration_ms) if duration_ms is not None else None,
                duration_basis='last_decoded_frame_end' if duration_ms is not None else 'unknown',
                decoded_frame_count=len(records), last_frame_ms=float(last[2]),
                timestamp_origin='first_decoded_frame', frames=frames)
    except Exception:
        # Decoder errors can embed filenames/container text. Retain a code.
        return disposition('failed', 'video_invalid_or_undecodable', **metadata)


def _child():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
        except (ValueError, OSError):
            if sys.platform != 'darwin' or '--rss-guarded' not in sys.argv:
                raise
    except (ImportError, ValueError, OSError):
        result = disposition('unsupported', 'video_resource_limits_unavailable')
    else:
        target = None if sys.argv[1] == 'sample' else int(sys.argv[1])
        result = _decode(sys.stdin.buffer.read(MAX_VIDEO_BYTES + 1), target)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())


async def decode_video(data, requested_ms=None):
    """Read only supplied bytes; bound and reap the exact decoder child."""
    from .documents import _MemoryGuardError, _sample_rss, _watch_rss, _memory_metadata
    if requested_ms is not None and (type(requested_ms) is not int or not 0 <= requested_ms <= MAX_DURATION_MS):
        raise ValueError('invalid_video_time')
    guarded = sys.platform == 'darwin'
    if guarded:
        try:
            import psutil
        except ImportError:
            return disposition('unsupported', 'video_memory_monitor_unavailable')
    process = await asyncio.create_subprocess_exec(sys.executable, '-I', str(Path(__file__).resolve()),
        str(requested_ms) if requested_ms is not None else 'sample', *(['--rss-guarded'] if guarded else []),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    communication = watcher = target = None
    memory = _memory_metadata('sampled_rss' if guarded else 'address_space')
    try:
        async with asyncio.timeout(MAX_DECODE_SECONDS):
            if guarded:
                target = psutil.Process(process.pid); _sample_rss(target)
                watcher = asyncio.create_task(_watch_rss(process, target))
            communication = asyncio.create_task(process.communicate(data))
            if watcher:
                await asyncio.wait((communication, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher.done(): watcher.result()
            raw, _ = await asyncio.shield(communication)
        if process.returncode or len(raw) > MAX_OUTPUT_BYTES:
            return disposition('failed', 'video_decoder_exit_or_output_limit', **memory)
        result = json.loads(raw)
        if result.get('version') != VERSION or result.get('status') not in {'complete', 'unsupported', 'failed'}:
            raise ValueError('invalid_decoder_result')
        return result | memory
    except asyncio.TimeoutError:
        return disposition('failed', 'video_decoder_time_limit', **memory)
    except _MemoryGuardError:
        return disposition('unsupported', 'video_decoder_memory_limit', **memory)
    except (ValueError, TypeError):
        return disposition('failed', 'video_decoder_invalid_result', **memory)
    finally:
        if process.returncode is None:
            try: process.kill()
            except ProcessLookupError: pass
        if watcher: watcher.cancel()
        if communication is None: communication = asyncio.create_task(process.communicate())
        await asyncio.gather(communication, *([watcher] if watcher else []), return_exceptions=True)
        await process.wait()


if __name__ == '__main__':
    _child()
