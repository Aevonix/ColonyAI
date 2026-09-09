"""Open canonical evidence for the current participant, without an authority grant."""
import json
import re


def handle(args, scope, client, request_memory, context):
    if (scope is None or not scope.valid_participant or not scope.task_id or not scope.turn_id
            or not context.get('tool_call_id')):
        return json.dumps({'error': 'An exact native participant and tool call are required'})
    allowed = {'source_id', 'source_version', 'view', 'claim_id', 'offset', 'read_revision'}
    if (not isinstance(args, dict) or set(args) - allowed
            or not isinstance(args.get('source_id'), str) or not 1 <= len(args['source_id']) <= 256
            or not re.fullmatch('[0-9a-f]{64}', str(args.get('source_version', '')))
            or args.get('view', 'source') not in {'source', 'assertions'}
            or type(args.get('offset', 0)) is not int or not 0 <= args.get('offset', 0) <= 10000000
            or args.get('read_revision') is not None and not re.fullmatch('[0-9a-f]{64}', str(args['read_revision']))):
        return json.dumps({'error': 'Supply an exact source revision and bounded read selector'})
    ref = {key: args[key] for key in ('source_id', 'source_version')}
    if ref not in (request_memory.supplied_snapshot(scope) or []):
        return json.dumps({'error': 'The source revision must have been supplied to this participant and turn'})
    try:
        response = client.post('/v1/host/memory/read', timeout=3, json={
            'identity': {'host_id': 'hermes'}, 'person_id': scope.contact_id, 'session_id': scope.session_id,
            'source_id': args['source_id'], 'source_version': args['source_version'],
            'source_view': args.get('view', 'source'), 'claim_id': args.get('claim_id'),
            'offset': args.get('offset', 0), 'read_revision': args.get('read_revision')})
        response.raise_for_status()
        result = response.json()['source']
        refs = result['source_refs']
        if (type(result['watermark']) is not int or result['watermark'] < 0 or not isinstance(refs, list)
                or ref not in refs or any(not isinstance(r, dict) or set(r) != {'source_id', 'source_version'}
                    or not isinstance(r['source_id'], str) or not 1 <= len(r['source_id']) <= 256
                    or not re.fullmatch('[0-9a-f]{64}', str(r['source_version'])) for r in refs)):
            raise ValueError('invalid_source_read_lineage')
        text = json.dumps({**result, 'colony_source_read_v1': True}, ensure_ascii=False)
        if not request_memory.register_source_read(scope, context['tool_call_id'], text, result):
            raise ValueError('source_read_turn_expired')
        return text
    except Exception:
        return json.dumps({'error': 'Source read unavailable or changed; refresh scoped recall before continuing',
                           'complete': False})
