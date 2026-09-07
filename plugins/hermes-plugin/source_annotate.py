"""Append attributed correction evidence for an actually supplied source revision."""
import hashlib
import json
import re


def handle(args, scope, client, request_memory):
    if (scope is None or not scope.valid_participant
            or scope.authority_lane not in {'owner', 'system'}
            or not scope.task_id or not scope.turn_id):
        return json.dumps({'error': 'An attested owner or system turn is required'})
    keys = {'source_id', 'source_version', 'excerpt', 'correction'}
    if (not isinstance(args, dict) or set(args) != keys
            or any(not isinstance(args[k], str) or not args[k].strip() for k in keys)
            or len(args['source_id']) > 256
            or not re.fullmatch(r'[0-9a-f]{64}', args['source_version'])
            or any(len(args[k]) > 4096 for k in ('excerpt', 'correction'))):
        return json.dumps({'error': 'Supply only an exact source ID/version, excerpt and correction'})
    ref = {k: args[k] for k in ('source_id', 'source_version')}
    if ref not in (request_memory.supplied_snapshot(scope) or []):
        return json.dumps({'error': 'The exact source revision must have been supplied to this turn'})
    identity = json.dumps([scope.contact_id, scope.session_id, scope.turn_id, args],
                          sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    annotation_id = 'native-annotation:' + hashlib.sha256(identity.encode()).hexdigest()
    try:
        response = client.post('/v1/host/memory/sources/annotations', timeout=3,
            json={**args, 'annotation_id': annotation_id, 'contact_id': scope.contact_id,
                  'session_id': scope.session_id})
        if response.status_code in {403, 409, 422}:
            return json.dumps({'error': 'The source annotation was rejected; inspect current scoped evidence',
                               'accepted': False, 'status_code': response.status_code})
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception:
        # The server may have committed before the acknowledgement was lost.
        # Retrying the identical arguments in this turn reuses the same ID.
        return json.dumps({'error': 'Source annotation is unconfirmed; retry identical arguments in this turn',
                           'annotation_id': annotation_id, 'confirmation': 'unknown'})
