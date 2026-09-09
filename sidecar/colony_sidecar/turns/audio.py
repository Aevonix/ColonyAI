"""Bounded supplied audio and fallible transcript segments, never remote fetches."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import io
import math
import wave

MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 60


def decode_audio(block):
    item = block.get('input_audio')
    if (not isinstance(item, dict) or set(item) != {'data', 'format'}
            or item['format'] != 'wav' or not isinstance(item['data'], str)
            or len(item['data']) > MAX_AUDIO_BYTES * 4 // 3 + 16):
        raise ValueError('unsupported_or_oversized_audio')
    data = base64.b64decode(item['data'], validate=True)
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError('oversized_audio')
    with wave.open(io.BytesIO(data), 'rb') as clip:
        rate, frames = clip.getframerate(), clip.getnframes()
        if (clip.getnchannels() != 1 or clip.getsampwidth() != 2 or clip.getcomptype() != 'NONE'
                or not 8000 <= rate <= 48000 or not 0 < frames <= rate * MAX_AUDIO_SECONDS
                or len(clip.readframes(frames + 1)) != frames * 2):
            raise ValueError('unsupported_audio_encoding_or_duration')
    return data, {'sample_rate': rate, 'channels': 1, 'sample_width_bytes': 2,
                  'duration_ms': frames * 1000 / rate, 'frame_count': frames}


def transcript(block, data, duration_ms):
    """Validate supplied recognition lineage; clocks are not inferred from text."""
    if (not isinstance(block, dict) or set(block) != {'type', 'audio_sha256', 'segments',
            'recognizer', 'received_at', 'captured_at'} or block['type'] != 'audio_transcript'
            or block['audio_sha256'] != hashlib.sha256(data).hexdigest()):
        raise ValueError('audio_transcript_parent_mismatch')
    segments = block['segments']
    if not isinstance(segments, list) or not 1 <= len(segments) <= 32:
        raise ValueError('unsupported_transcript_segments')
    previous = 0
    for segment in segments:
        if (not isinstance(segment, dict) or set(segment) != {'start_ms', 'end_ms', 'text'}
                or not isinstance(segment['text'], str) or not 1 <= len(segment['text']) <= 12000
                or any(type(segment[key]) not in (int, float) or not math.isfinite(segment[key])
                       for key in ('start_ms', 'end_ms'))
                or not previous <= segment['start_ms'] < segment['end_ms'] <= duration_ms + .001):
            raise ValueError('invalid_transcript_segment')
        previous = segment['end_ms']
    for key in ('received_at', 'captured_at'):
        value = block[key]
        if value is None and key == 'captured_at':
            continue
        if not isinstance(value, str) or len(value) > 64 or datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('invalid_audio_clock')
    model = block['recognizer']
    if (not isinstance(model, dict) or set(model) != {'model_id', 'model_revision'}
            or any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in model.values())):
        raise ValueError('invalid_recognizer_provenance')
    return {**block, 'asset_id': 'sha256:' + block['audio_sha256'],
            'epistemic_state': 'derived_unverified', 'timing_basis': 'supplied_clip_offsets',
            'confidence': None}


def source_text(content):
    """Text-only recall of retained evidence; raw audio never enters prompts."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get('type') in {'text', 'input_text', 'output_text'} and isinstance(block.get('text'), str):
            parts.append(block['text'])
        elif block.get('type') == 'audio_transcript' and block.get('epistemic_state') == 'derived_unverified':
            for segment in block.get('segments', []):
                parts.append(f"Unverified machine transcript [{segment['start_ms'] / 1000:.3f}.."
                             f"{segment['end_ms'] / 1000:.3f}s of {block['asset_id']}]: {segment['text']}")
    return '\n'.join(parts)


def evidence_metadata(message):
    content = message.get('content')
    if isinstance(content, list) and any(isinstance(block, dict) and block.get('type') == 'audio_transcript'
            and block.get('epistemic_state') == 'derived_unverified' for block in content):
        return {'epistemic_state': 'derived_unverified', 'source_modality': 'audio_transcript'}
    return {}
