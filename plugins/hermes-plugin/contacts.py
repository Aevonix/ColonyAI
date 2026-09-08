"""Contact inspection and exact owner identity corrections over native scope."""
import hashlib
import json


def handle(args, scope, client):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review'}):
        return json.dumps({'error': 'An attested owner conversation is required'})
    try:
        if args.get('operation') == 'inspect' and set(args) <= {'operation', 'subject_contact_id', 'offset'}:
            response = client.get('/v1/host/social/contacts', timeout=3, params={
                'contact_id': scope.contact_id, 'subject_id': args.get('subject_contact_id', ''),
                'offset': args.get('offset', 0)})
        elif args.get('operation') == 'correct_identity' and set(args) <= {
                'operation', 'gateway', 'address', 'expected_contact_id', 'subject_contact_id', 'source_ids'}:
            if not scope.turn_id or not scope.session_id or not scope.user_message.strip():
                raise ValueError('owner_correction_turn_required')
            identity = [scope.contact_id, scope.session_id, scope.turn_id, args]
            operation_id = 'native-identity:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            response = client.post('/v1/host/social/contacts/correct-identity', timeout=5, json={
                'contact_id': scope.contact_id, 'operation_id': operation_id,
                'gateway': args['gateway'], 'address': args['address'],
                'expected_contact_id': args.get('expected_contact_id'),
                'subject_id': args.get('subject_contact_id'), 'source_ids': args.get('source_ids', []),
                'evidence_refs': ['native-owner-turn:' + scope.turn_id]})
        else:
            raise ValueError('invalid_contact_operation')
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception:
        return json.dumps({'error': 'Contact operation unavailable; inspect current state before retrying'})
