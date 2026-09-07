"""Explicit owner source selection through the existing scoped erasure API."""
import json


def handle(args, scope, client):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review'}
            or not scope.turn_id or not scope.user_message.strip()):
        return json.dumps({'error': 'An attested owner turn requesting source removal is required'})
    ids = args.get('source_ids')
    if (set(args) != {'source_ids'} or not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in ids)):
        return json.dumps({'error': 'Select 1..100 exact retained source IDs from memory provenance'})
    try:
        # Identity comes from transport, never from a model argument. The
        # backend validates ownership and rejects unknown/foreign IDs atomically.
        response = client.post('/v1/host/memory/sources/forget', timeout=3,
            json={'contact_id': scope.contact_id, 'source_ids': list(dict.fromkeys(ids))})
        if response.status_code in {409, 422}:
            return json.dumps({'source_erased': False, 'error': 'The selected sources are unavailable or ambiguous; inspect their provenance'})
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception:
        return json.dumps({'error': 'Source removal is unconfirmed; inspect source state before retrying'})
