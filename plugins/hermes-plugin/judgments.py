"""Owner judgment controls using the existing native transport identity."""
import hashlib
import json


def handle(args, scope, client):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review'}):
        return json.dumps({'error': 'An attested owner conversation is required'})
    operation = args.get('operation')
    if 'appraisal_id' in args or 'subject_contact_id' in args:
        return _appraisals(args, scope, client)
    expected = {'operation'} if operation == 'inspect' else {'operation', 'judgment_id'}
    if operation == 'reconsider':
        expected.add('source_id')
    if operation not in {'inspect', 'withdraw', 'reconsider'} or set(args) != expected:
        return json.dumps({'error': 'Use inspect, withdraw with an exact judgment ID, or reconsider with an ID and retained source ID'})
    try:
        if operation == 'inspect':
            response = client.get("/v1/host/self", timeout=3)
            response.raise_for_status()
            perspective = response.json().get('perspective')
            if not isinstance(perspective, dict):
                raise ValueError('self_perspective_unavailable')
            keys = ('judgments', 'judgment_history', 'judgment_processing')
            return json.dumps({key: perspective.get(key, [])[:10] for key in keys} |
                              {'truncated': {key: len(perspective.get(key, [])) > 10 for key in keys},
                               'appraisals': perspective.get('appraisals', {}),
                               'judgments_enabled': perspective.get('judgments_enabled')})
        if (type(args['judgment_id']) is not int or args['judgment_id'] < 1 or
                not scope.turn_id or not scope.user_message.strip()):
            raise ValueError('owner_turn_and_judgment_required')
        if operation == 'reconsider' and (not isinstance(args['source_id'], str) or not 1 <= len(args['source_id']) <= 256):
            raise ValueError('retained_source_id_required')
        # The model selects only a target/operation. Identity and the owner's
        # actual instruction come from the native turn, never tool arguments.
        identity = [scope.contact_id, scope.session_id, scope.turn_id, args]
        correction_id = 'native-judgment:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        response = client.post("/v1/host/learning/correction", timeout=3, json={
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


def _appraisals(args, scope, client):
    operation = args.get('operation')
    try:
        if operation == 'inspect' and set(args) == {'operation', 'subject_contact_id'}:
            subject = args['subject_contact_id']
            if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
                raise ValueError('exact_contact_required')
            response = client.get('/v1/host/social/appraisals', timeout=3,
                params={'contact_id': scope.contact_id, 'subject_id': subject, 'history': True})
        elif operation in {'withdraw', 'reconsider'} and set(args) == {'operation', 'appraisal_id'}:
            identifier = args['appraisal_id']
            if (not isinstance(identifier, str) or not identifier.startswith('appraisal:')
                    or len(identifier) > 192 or not scope.turn_id or not scope.user_message.strip()):
                raise ValueError('owner_turn_and_appraisal_required')
            identity = [scope.contact_id, scope.session_id, scope.turn_id, args]
            correction_id = 'native-appraisal:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            response = client.post('/v1/host/social/appraisals/correct', timeout=3, json={
                'contact_id': scope.contact_id, 'record_id': identifier, 'action': operation,
                'correction_id': correction_id, 'reason': scope.user_message[:1500]})
        else:
            raise ValueError('invalid_appraisal_operation')
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception:
        return json.dumps({'error': 'Appraisal control unavailable; no change is confirmed'})
