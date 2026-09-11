"""Open canonical evidence for the current participant, without an authority grant."""
import json
import re
import base64
import hashlib
import math
import time


def handle(args, scope, client, request_memory, context):
    if (scope is None or not scope.valid_participant or not scope.task_id or not scope.turn_id
            or not context.get('tool_call_id')):
        return json.dumps({'error': 'An exact native participant and tool call are required'})
    allowed = {'source_id', 'source_version', 'view', 'claim_id', 'offset', 'read_revision', 'asset_hash', 'page', 'requested_ms'}
    if (not isinstance(args, dict) or set(args) - allowed
            or not isinstance(args.get('source_id'), str) or not 1 <= len(args['source_id']) <= 256
            or not re.fullmatch('[0-9a-f]{64}', str(args.get('source_version', '')))
            or args.get('view', 'source') not in {'source', 'assertions', 'image', 'document', 'video'}
            or type(args.get('offset', 0)) is not int or not 0 <= args.get('offset', 0) <= 10000000
            or args.get('read_revision') is not None and not re.fullmatch('[0-9a-f]{64}', str(args['read_revision']))):
        return json.dumps({'error': 'Supply an exact source revision and bounded read selector'})
    image_view = args.get('view') == 'image'
    document_view = args.get('view') == 'document'
    video_view = args.get('view') == 'video'
    if ((image_view and (not re.fullmatch('[0-9a-f]{64}', str(args.get('asset_hash', '')))
                        or args.get('claim_id') or args.get('offset') or args.get('read_revision')))
            or not image_view and not document_view and not video_view and args.get('asset_hash') is not None):
        return json.dumps({'error': 'Image opening requires an exact asset hash and no history/page selector'})
    if ((document_view and (not re.fullmatch('[0-9a-f]{64}', str(args.get('asset_hash', '')))
                           or type(args.get('page')) is not int or args['page'] < 1 or args.get('claim_id')
                           or args.get('offset') and not args.get('read_revision')))
            or not document_view and args.get('page') is not None):
        return json.dumps({'error': 'Document opening requires an exact asset hash, original page number, and revision for continuation'})
    if ((video_view and (not re.fullmatch('[0-9a-f]{64}', str(args.get('asset_hash', '')))
                        or type(args.get('requested_ms')) is not int or not 0 <= args['requested_ms'] <= 30000
                        or set(args) & {'claim_id', 'offset', 'read_revision', 'page'}))
            or not video_view and args.get('requested_ms') is not None):
        return json.dumps({'error': 'Video opening requires the original clip hash and clip-relative requested_ms in 0..30000, without a history/page selector'})
    ref = {key: args[key] for key in ('source_id', 'source_version')}
    supplied = request_memory.supplied_snapshot(scope) or []
    if ref not in supplied:
        error = {'error': 'The source revision must have been supplied to this participant and turn'}
        matching = [candidate for candidate in supplied if candidate['source_version'] == args['source_version']]
        if matching:
            error.update(matching_supplied_sources=matching[:4], guidance=(
                'Copy the matching canonical source_id and source_version together from recalled provenance. '
                'A memory row ID or asset ID is not a source ID. No source was opened.'))
        return json.dumps(error)
    try:
        deadline = time.monotonic() + 20 if video_view else None
        response = client.post('/v1/host/memory/read', timeout=20 if video_view else 3,
            **({'_deadline_monotonic': deadline} if video_view else {}), json={
            'identity': {'host_id': 'hermes'}, 'person_id': scope.contact_id, 'session_id': scope.session_id,
            'source_id': args['source_id'], 'source_version': args['source_version'],
            'source_view': args.get('view', 'source'),
            **({} if video_view else {'claim_id': args.get('claim_id'),
                'offset': args.get('offset', 0), 'read_revision': args.get('read_revision')}),
            **({'asset_hash': args['asset_hash']} if image_view or document_view or video_view else {}),
            **({'page': args['page']} if document_view else {}),
            **({'requested_ms': args['requested_ms']} if video_view else {})})
        response.raise_for_status()
        result = response.json()['source']
        refs = result['source_refs']
        if (type(result['watermark']) is not int or result['watermark'] < 0 or not isinstance(refs, list)
                or ref not in refs or any(not isinstance(r, dict) or set(r) != {'source_id', 'source_version'}
                    or not isinstance(r['source_id'], str) or not 1 <= len(r['source_id']) <= 256
                    or not re.fullmatch('[0-9a-f]{64}', str(r['source_version'])) for r in refs)):
            raise ValueError('invalid_source_read_lineage')
        image_url = None
        if document_view:
            document = result.get('document', {})
            if (not isinstance(document, dict) or document.get('asset_hash') != args['asset_hash']
                    or document.get('page') != args['page'] or document.get('mime_type') != 'application/pdf'
                    or document.get('status') not in {'pending', 'complete', 'partial', 'unsupported', 'failed'}
                    or document.get('ocr_performed') is not False
                    or result.get('view') != 'document' or result.get('source_id') != args['source_id']
                    or result.get('source_version') != args['source_version']
                    or result.get('offset') != args.get('offset', 0) or result.get('offset_unit') != 'characters'
                    or not re.fullmatch('[0-9a-f]{64}', str(result.get('read_revision', '')))
                    or args.get('read_revision') is not None and result['read_revision'] != args['read_revision']
                    or not isinstance(result.get('content'), str) or len(result['content']) > 4096):
                raise ValueError('invalid_source_document')
        if video_view:
            video = result.get('video', {})
            actual_ms = video.get('actual_ms')
            time_base = str(video.get('time_base', ''))
            if (not isinstance(video, dict) or video.get('asset_hash') != args['asset_hash']
                    or video.get('mime_type') != 'video/mp4' or video.get('requested_ms') != args['requested_ms']
                    or type(video.get('requested_ms')) is not int
                    or type(actual_ms) not in (int, float) or not math.isfinite(actual_ms)
                    or not args['requested_ms'] <= actual_ms <= 30000
                    or type(video.get('frame_pts')) is not int
                    or type(video.get('origin_pts')) is not int
                    or not re.fullmatch(r'[1-9][0-9]*/[1-9][0-9]*', time_base)
                    or not re.fullmatch(r'[1-9][0-9]*/[1-9][0-9]*', str(video.get('origin_time_base', '')))
                    or type(video.get('stream_index')) is not int or video['stream_index'] < 0
                    or video.get('decoder') != 'PyAV'
                    or not isinstance(video.get('decoder_version'), str) or not 1 <= len(video['decoder_version']) <= 64
                    or video.get('selection') != 'first_frame_at_or_after'
                    or video.get('timestamp_origin') != 'first_decoded_frame'
                    or video.get('audio_processed') is not False
                    or any(type(video.get(field)) is not int or video[field] < 1
                           for field in ('source_width', 'source_height', 'width', 'height'))
                    or video.get('transform') != 'rgb24_png_no_resize'
                    or result.get('source_id') != args['source_id'] or result.get('source_version') != args['source_version']):
                raise ValueError('invalid_source_video')
        if image_view or video_view:
            image = dict(result['image'])
            image_url = image.pop('data_url')
            match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)', image_url)
            if (not match or len(match[2]) > (4 * 1024 * 1024 * 4 // 3 + 16)
                    or not re.fullmatch('[0-9a-f]{64}', str(image.get('asset_hash', '')))
                    or image_view and image.get('asset_hash') != args['asset_hash']
                    or video_view and image.get('mime_type') != 'image/png'
                    or image.get('mime_type') != match[1]
                    or result.get('view') != args['view'] or result.get('image_bytes_included') is not True
                    or not re.fullmatch('[0-9a-f]{64}', str(result.get('read_revision', '')))
                    or not isinstance(result.get('content'), str) or len(result['content']) > 16384):
                raise ValueError('invalid_source_image')
            data = base64.b64decode(match[2], validate=True)
            if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != image['asset_hash']:
                raise ValueError('invalid_source_image_bytes')
            result = {**result, 'image': image}
        text = json.dumps({**result, 'colony_source_read_v1': True}, ensure_ascii=False)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('source_video_open_deadline')
        if not request_memory.register_source_read(scope, context['tool_call_id'], text, result, image_url=image_url):
            raise ValueError('source_read_turn_expired')
        if image_url:
            return {'_multimodal': True, 'content': [{'type': 'text', 'text': text},
                    {'type': 'image_url', 'image_url': {'url': image_url}}],
                    'text_summary': json.dumps({'error': 'This processor or tool-result adapter did not include the original image. '
                        'No visual inspection occurred; use a vision-capable processor with image tool results.',
                        'complete': False, 'image_bytes_included': False})}
        return text
    except Exception:
        return json.dumps({'error': 'Source read unavailable or changed; refresh scoped recall before continuing',
                           'complete': False})
