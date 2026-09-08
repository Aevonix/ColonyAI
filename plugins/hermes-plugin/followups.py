"""Bind expected replies to the native turn's existing undertaking."""
import hashlib
import json
from urllib.parse import quote


def capture_instruction(scope, client):
    versions = {}
    # Capture a direct owner instruction now when it is the first turn
    # of the task. Ordinary completion still captures the complete turn;
    # this source-only copy never runs another learning/extraction pass.
    if scope.platform not in {'cron', 'subagent', 'background_review'} and scope.user_message.strip():
        material = [scope.contact_id, scope.session_id, scope.turn_id, scope.user_message]
        source_id = 'task-instruction:' + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        accepted = client.sync_turn(session_id=scope.session_id, contact_id=scope.contact_id,
            turn_id=source_id, user_message=scope.user_message, source_only=True,
            instruction_only=True,
            require_source_receipt=True, sender={'platform': scope.platform, 'user_id': scope.sender_id},
            timeout_seconds=1)
        if not accepted:
            raise ValueError('Task instruction capture is unconfirmed; retry the same operation')
        messages = [{'role': 'user', 'content': scope.user_message}]
        versions[source_id] = hashlib.sha256(json.dumps(messages, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return versions


def handle(args, scope, client, coordinator, request_memory, context):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or not scope.turn_id):
        return json.dumps({'error': 'An attested owner task is required'})
    try:
        operation = args.get('operation')
        if operation == 'inspect' and set(args) == {'operation', 'wait_id'}:
            response = client.get('/v1/host/temporal-followups/'+quote(args['wait_id'], safe=''),
                params={'contact_id': scope.contact_id}, timeout=3)
        elif operation in {'cancel', 'defer'} and set(args) == ({'operation', 'wait_id', 'until'} if operation == 'defer' else {'operation', 'wait_id'}):
            response = client.post('/v1/host/temporal-followups/'+quote(args['wait_id'], safe='')+'/change',
                json={'contact_id': scope.contact_id, 'operation': operation,
                      'evidence_ref': 'native-owner-turn:'+scope.turn_id, 'until': args.get('until')}, timeout=3)
        elif operation == 'expect_reply' and set(args) <= {'operation', 'commitment_id', 'recipient_id',
                'outbound_ref', 'expected_after_seconds', 'expires_at', 'source_ids', 'timezone_name'}:
            held = coordinator.handoff(args['commitment_id'], context)
            if held is None:
                raise ValueError('Claim the existing commitment before recording an expected reply')
            available = {r['source_id']: r['source_version'] for r in request_memory.supplied_snapshot(scope) or []}
            selected = args.get('source_ids', [])
            if not isinstance(selected, list) or any(s not in available for s in selected):
                raise ValueError('Use retained source IDs actually supplied to this task')
            versions = {s: available[s] for s in selected}
            versions.update(capture_instruction(scope, client))
            response = client.post('/v1/host/temporal-followups', timeout=3, json={
                'contact_id': scope.contact_id, 'recipient_id': args['recipient_id'],
                'commitment_id': args['commitment_id'], 'work_id': held['task_id'],
                'session_id': held['session_id'], 'turn_id': held['turn_id'], 'claim_id': held['claim_id'],
                'outbound_ref': args['outbound_ref'], 'source_refs': list(versions), 'source_versions': versions,
                'expected_after_seconds': args['expected_after_seconds'], 'expires_at': args['expires_at'],
                'timezone_name': args.get('timezone_name')})
        else:
            raise ValueError('Use expect_reply, inspect, cancel or defer with exact task references')
        response.raise_for_status()
        return json.dumps(response.json())
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({'error': str(exc), 'effect_authorized': False})
    except Exception:
        return json.dumps({'error': 'Waiting state is unconfirmed; retry the same operation', 'effect_authorized': False})


def prepare_message_plan(args, scope, client, coordinator, request_memory, context, outbound_ref, channel):
    """Prepare immutable text/timing under the currently held original owner task."""
    required = {'commitment_id', 'recipient_id', 'purpose', 'message', 'expected_after_seconds', 'expires_at'}
    if (not isinstance(args, dict) or not required <= set(args)
            or set(args) - required - {'timezone_name'} or coordinator is None
            or request_memory is None or scope.authority_lane != 'owner'
            or scope.platform in {'cron', 'subagent', 'background_review'}):
        raise ValueError('Prepared followup requires the original authenticated owner task')
    held = coordinator.handoff(args['commitment_id'], context)
    if held is None:
        raise ValueError('Claim the original task before preparing its followup')
    result = json.loads(handle({key: value for key, value in {
        'operation': 'expect_reply', 'commitment_id': args['commitment_id'],
        'recipient_id': args['recipient_id'], 'outbound_ref': outbound_ref,
        'expected_after_seconds': args['expected_after_seconds'], 'expires_at': args['expires_at'],
        'timezone_name': args.get('timezone_name')}.items() if value is not None},
        scope, client, coordinator, request_memory, context))
    if 'wait_id' not in result:
        raise ValueError(result.get('error', 'Expected reply registration is unconfirmed'))
    instructions = capture_instruction(scope, client)
    if len(instructions) != 1:
        raise ValueError('Direct owner task instruction required')
    source_id, source_version = next(iter(instructions.items()))
    message = args['message']
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise ValueError('A bounded prepared followup message is required')
    plan = dict(wait_id=result['wait_id'], commitment_id=args['commitment_id'], work_id=held['task_id'],
        source_id=source_id, source_version=source_version, recipient_id=args['recipient_id'], channel=channel,
        purpose=args['purpose'], expires_at=float(args['expires_at']), max_followups=1,
        message=message, message_sha256=hashlib.sha256(message.encode()).hexdigest())
    response = client.post('/v1/host/temporal-followups/'+quote(result['wait_id'], safe='')+'/bind-plan',
        json={'contact_id': scope.contact_id, 'session_id': held['session_id'], 'turn_id': held['turn_id'],
              'claim_id': held['claim_id'], 'plan': plan}, timeout=3)
    response.raise_for_status()
    if not response.json().get('bound'):
        raise ValueError('Prepared followup binding is unconfirmed')
    return plan
