"""Open canonical evidence for the current participant, without an authority grant."""
import json
import re
import base64
import hashlib


def handle(args, scope, client, request_memory, context):
    if (scope is None or not scope.valid_participant or not scope.task_id or not scope.turn_id
            or not context.get('tool_call_id')):
        return json.dumps({'error': 'An exact native participant and tool call are required'})
    allowed = {'source_id', 'source_version', 'view', 'claim_id', 'offset', 'read_revision', 'asset_hash', 'page'}
    if (not isinstance(args, dict) or set(args) - allowed
            or not isinstance(args.get('source_id'), str) or not 1 <= len(args['source_id']) <= 256
            or not re.fullmatch('[0-9a-f]{64}', str(args.get('source_version', '')))
            or args.get('view', 'source') not in {'source', 'assertions', 'image', 'document'}
            or type(args.get('offset', 0)) is not int or not 0 <= args.get('offset', 0) <= 10000000
            or args.get('read_revision') is not None and not re.fullmatch('[0-9a-f]{64}', str(args['read_revision']))):
        return json.dumps({'error': 'Supply an exact source revision and bounded read selector'})
    image_view = args.get('view') == 'image'
    document_view = args.get('view') == 'document'
    if ((image_view and (not re.fullmatch('[0-9a-f]{64}', str(args.get('asset_hash', '')))
                        or args.get('claim_id') or args.get('offset') or args.get('read_revision')))
            or not image_view and not document_view and args.get('asset_hash') is not None):
        return json.dumps({'error': 'Image opening requires an exact asset hash and no history/page selector'})
    if ((document_view and (not re.fullmatch('[0-9a-f]{64}', str(args.get('asset_hash', '')))
                           or type(args.get('page')) is not int or args['page'] < 1 or args.get('claim_id')
                           or args.get('offset') and not args.get('read_revision')))
            or not document_view and args.get('page') is not None):
        return json.dumps({'error': 'Document opening requires an exact asset hash, original page number, and revision for continuation'})
    ref = {key: args[key] for key in ('source_id', 'source_version')}
    if ref not in (request_memory.supplied_snapshot(scope) or []):
        return json.dumps({'error': 'The source revision must have been supplied to this participant and turn'})
    try:
        response = client.post('/v1/host/memory/read', timeout=3, json={
            'identity': {'host_id': 'hermes'}, 'person_id': scope.contact_id, 'session_id': scope.session_id,
            'source_id': args['source_id'], 'source_version': args['source_version'],
            'source_view': args.get('view', 'source'), 'claim_id': args.get('claim_id'),
            'offset': args.get('offset', 0), 'read_revision': args.get('read_revision'),
            **({'asset_hash': args['asset_hash']} if image_view or document_view else {}),
            **({'page': args['page']} if document_view else {})})
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
        if image_view:
            image = dict(result['image'])
            image_url = image.pop('data_url')
            match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)', image_url)
            if (not match or len(match[2]) > (4 * 1024 * 1024 * 4 // 3 + 16)
                    or image.get('asset_hash') != args['asset_hash'] or image.get('mime_type') != match[1]
                    or result.get('view') != 'image' or result.get('image_bytes_included') is not True
                    or not re.fullmatch('[0-9a-f]{64}', str(result.get('read_revision', '')))
                    or not isinstance(result.get('content'), str) or len(result['content']) > 16384):
                raise ValueError('invalid_source_image')
            data = base64.b64decode(match[2], validate=True)
            if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != args['asset_hash']:
                raise ValueError('invalid_source_image_bytes')
            result = {**result, 'image': image}
        text = json.dumps({**result, 'colony_source_read_v1': True}, ensure_ascii=False)
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
