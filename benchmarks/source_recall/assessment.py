"""Expected answers are used only after retrieval, never by candidate producers."""
import json

SCOPE = {'owner': ('private', 'team', 'public'), 'team-member': ('team', 'public'), 'guest': ('public',)}


def eligible_ids(records, query):
    visible = [r for r in records if r['scope'] in SCOPE[query['principal']] and r['at'] <= query['as_of']]
    blocked = {r['id'] for r in visible if r.get('deleted')}
    blocked.update(r['supersedes'] for r in visible if r.get('supersedes'))
    while True:
        expanded = blocked | {r['id'] for r in visible if set(r.get('parents', [])) & blocked}
        if expanded == blocked:
            break
        blocked = expanded
    return {r['id'] for r in visible if r['id'] not in blocked
            and (not query.get('since') or r['at'] >= query['since'])}


def assess(query, rows, records):
    ids, conflicts = [], []
    for row in rows:
        if row.get('atomic_evidence'):
            bundle = json.loads(row['content'])
            ids.extend(member['source'][5:] for member in bundle['assertions'])
            if bundle['status'] == 'unresolved_conflict':
                conflicts.append(bundle)
        elif str(row.get('source_uri', '')).startswith('turn:'):
            ids.append(row['source_uri'][5:])
    ids = list(dict.fromkeys(ids))
    expected = set(query['expected'])
    invalid = sorted(set(ids) - eligible_ids(records, query))
    forbidden = sorted(set(ids) & set(query.get('forbidden', [])))
    provenance = all(row.get('atomic_evidence') or row.get('source_uri') for row in rows)
    return {'ids': ids, 'recall': len(expected & set(ids)) / len(expected) if expected else None,
        'expected_found': expected.issubset(ids), 'invalid': invalid, 'forbidden': forbidden,
        'abstained': not rows if query.get('abstain') else None,
        'conflict_marked': bool(conflicts) if query.get('conflict') else None,
        'source_provenance': bool(provenance),
        'strict_pass': bool(expected.issubset(ids) and not invalid and not forbidden and provenance
            and (not query.get('abstain') or not rows) and (not query.get('conflict') or conflicts))}
