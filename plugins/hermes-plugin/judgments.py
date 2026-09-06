"""Owner judgment controls using the existing native transport identity."""
import hashlib
import json


def handle(args, scope, client):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review'}):
        return json.dumps({'error': 'An attested owner conversation is required'})
    operation = args.get('operation')
    expected = {'operation'} if operation == 'inspect' else {'operation', 'judgment_id'}
    if operation == 'reconsider':
        expected.add('source_id')
    if operation not in {'inspect', 'withdraw', 'reconsider'} or set(args) != expected:
        return json.dumps({'error': 'Use inspect, withdraw with an exact judgment ID, or reconsider with an ID and retained source ID'})
    try:
        if operation == 'inspect':
            response = client.get('/v1/host/self', timeout=3)
            response.raise_for_status()
            perspective = response.json().get('perspective')
            if not isinstance(perspective, dict):
                raise ValueError('self_perspective_unavailable')
            keys = ('judgments', 'judgment_history', 'judgment_processing')
            return json.dumps({key: perspective.get(key, [])[:10] for key in keys} |
                              {'truncated': {key: len(perspective.get(key, [])) > 10 for key in keys}})
        if (type(args['judgment_id']) is not int or args['judgment_id'] < 1 or
                not scope.turn_id or not scope.user_message.strip()):
            raise ValueError('owner_turn_and_judgment_required')
        if operation == 'reconsider' and (not isinstance(args['source_id'], str) or not 1 <= len(args['source_id']) <= 256):
            raise ValueError('retained_source_id_required')
        # The model selects only a target/operation. Identity and the owner's
        # actual instruction come from the native turn, never tool arguments.
        identity = [scope.contact_id, scope.session_id, scope.turn_id, args]
        correction_id = 'native-judgment:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        response = client.post('/v1/host/learning/correction', timeout=3, json={
            'identity': {'host_id': 'hermes'},
            'context': {'contact_id': scope.contact_id, 'session_id': scope.session_id, 'turn_id': scope.turn_id},
            'original': '', 'correction': scope.user_message[:1500],
            'correction_id': correction_id, 'judgment_id': args['judgment_id'],
            'judgment_action': operation, 'source_id': args.get('source_id')})
        if response.status_code == 409:
            return json.dumps({'accepted': False, 'reason': 'The judgment changed or the source is unavailable; inspect current state before reconsidering'})
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception:
        return json.dumps({'error': 'Judgment control unavailable; no change is confirmed'})
