"""Search canonical evidence for the exact native participant and session."""
import json
import re
import time

from httpx import HTTPStatusError


def _reference(ref):
    return (isinstance(ref, dict) and set(ref) == {'source_id', 'source_version'}
        and isinstance(ref['source_id'], str) and 1 <= len(ref['source_id']) <= 256
        and re.fullmatch('[0-9a-f]{64}', str(ref['source_version'])))


def _annotation_check(check, refs):
    if not isinstance(check, dict) or set(check) != {'source_refs', 'message_hashes', 'annotation_ids'}:
        return False
    parents, membership, notes = check['source_refs'], check['message_hashes'], check['annotation_ids']
    return (isinstance(parents, list) and 1 <= len(parents) <= 512
        and all(_reference(ref) and ref in refs for ref in parents)
        and isinstance(membership, dict) and set(membership) == {ref['source_id'] for ref in parents}
        and all(isinstance(hashes, list) and len(hashes) <= 512
            and all(isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for h in hashes)
            for hashes in membership.values())
        and isinstance(notes, list) and len(notes) <= 512
        and all(isinstance(note, str) and 1 <= len(note) <= 256 for note in notes))


def handle(args, scope, client, request_memory, context):
    if (scope is None or not scope.valid_participant or not scope.task_id or not scope.turn_id
            or not context.get('tool_call_id') or request_memory is None):
        return json.dumps({'error': 'An exact native participant and tool call are required'})
    if (not isinstance(args, dict) or set(args) - {'query', 'limit'}
            or not isinstance(args.get('query'), str) or not args['query'].strip()
            or len(args['query']) > 4096
            or type(args.get('limit', 5)) is not int or not 1 <= args.get('limit', 5) <= 20):
        return json.dumps({'error': 'Supply a nonempty query of at most 4096 characters and a limit in 1..20'})
    deadline = time.monotonic() + 10
    try:
        response = client.post('/v1/host/memory/search', timeout=10, _deadline_monotonic=deadline,
            json={'identity': {'host_id': 'hermes'}, 'person_id': scope.contact_id,
                  'session_id': scope.session_id, 'query': args['query'], 'limit': args.get('limit', 5)})
        response.raise_for_status()
        result = response.json()
        refs, retrieval, checks = result['source_refs'], result['retrieval'], result['annotation_checks']
        if (not isinstance(result['content'], str)
                or type(result['count']) is not int or not 0 <= result['count'] <= args.get('limit', 5)
                or type(result['watermark']) is not int or result['watermark'] < 0
                or not isinstance(refs, list) or len(refs) > 512
                or any(not _reference(ref) for ref in refs)
                or not isinstance(checks, list) or len(checks) > 20
                or any(not _annotation_check(check, refs) for check in checks)
                or not isinstance(retrieval, dict)
                or retrieval.get('semantic') not in {'ready', 'unavailable', 'failed'}
                or retrieval.get('contact_facts') not in {'ready', 'unavailable', 'not_in_scope'}
                or result['count'] == 0 and (result['content'] or refs or checks)
                or refs and not checks):
            raise ValueError('invalid_canonical_search_response')
        text = json.dumps({**result, 'apsimo_memory_search_v1': True,
            'evidence_basis': 'recalled_excerpt', 'full_source_opened': False,
            'guidance': 'Search returns selected evidence excerpts. Preserve their speaker, time and corrections. '
                        'Open a returned source_id and source_version with the memory source reader '
                        'to inspect the retained original; search does not re-inspect its underlying subject.'},
            ensure_ascii=False)
        if time.monotonic() >= deadline:
            raise TimeoutError('canonical_memory_search_deadline')
        if not request_memory.register_source_search(scope, context['tool_call_id'], text, result):
            raise ValueError('memory_search_turn_expired')
        return text
    except HTTPStatusError as error:
        return json.dumps({'error': 'Canonical memory search is unavailable for this request',
                           'status': 'unavailable', 'status_code': error.response.status_code})
    except Exception:
        return json.dumps({'error': 'Canonical memory search is unavailable; no search evidence was supplied',
                           'status': 'unavailable'})
