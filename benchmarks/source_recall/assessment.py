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
            if 'assertions' in bundle:
                ids.extend(member['source'][5:] for member in bundle['assertions'])
                if bundle['status'] == 'unresolved_conflict':
                    conflicts.append(bundle)
            elif row.get('epistemic_state') == 'correction_evidence':
                # A real annotation bundle is assessed against its corrected
                # source, including when the annotation itself was retrieved.
                # Exact annotation IDs/versions remain in the captured rows.
                notes = bundle['corrections']
                annotation_ids = {note['source_id'] for note in notes}
                ids.extend(sid for sid in bundle['original'].get('source_ids', []) if sid not in annotation_ids)
                ids.extend(note['target']['source_id'] for note in notes)
            else:
                raise ValueError('Unknown atomic source evidence')
        elif str(row.get('source_uri', '')).startswith('turn:'):
            ids.append(row['source_uri'][5:])
    ids = list(dict.fromkeys(ids))
    expected = set(query['expected'])
    invalid = sorted(set(ids) - eligible_ids(records, query))
    forbidden = sorted(set(ids) & set(query.get('forbidden', [])))
    provenance = all(row.get('atomic_evidence') or row.get('source_uri') for row in rows)
    result = {'ids': ids, 'recall': len(expected & set(ids)) / len(expected) if expected else None,
        'expected_found': expected.issubset(ids), 'invalid': invalid, 'forbidden': forbidden,
        'abstained': not rows if query.get('abstain') else None,
        'conflict_marked': bool(conflicts) if query.get('conflict') else None,
        'source_provenance': bool(provenance),
        'strict_pass': bool(expected.issubset(ids) and not invalid and not forbidden and provenance
            and (not query.get('abstain') or not rows) and (not query.get('conflict') or conflicts))}
    # Optional independently authored labels measure usefulness separately
    # from source eligibility. They never enter ranking or source preparation.
    labels = query.get('relevance')
    if labels is not None:
        allowed = {'answer_useful', 'context_only', 'irrelevant'}
        if set(labels.values()) - allowed:
            raise ValueError('Unknown recall relevance label')
        result['relevance'] = {label: [sid for sid in ids if labels.get(sid) == label]
                               for label in sorted(allowed)}
        result['relevance']['unlabeled'] = [sid for sid in ids if sid not in labels]
        result['relevance']['irrelevant_selected'] = len(result['relevance']['irrelevant'])
        result['relevance']['answer_useful_selected'] = len(result['relevance']['answer_useful'])
        # Exact spans can expose a missing condition or correction even when
        # the owning source's ID survived. This is evidence coverage, not a
        # semantic answer grade; models may still misuse a complete packet.
        content = '\n'.join(row.get('content', '') for row in rows)
        result['required_evidence_present'] = all(span in content for span in query.get('required_evidence', []))
        result['useful_packet_pass'] = bool(result['strict_pass']
            and result['required_evidence_present']
            and not result['relevance']['irrelevant'] and not result['relevance']['unlabeled'])
        # An unanswered question may have relevant request or progress records.
        # They can explain what remains unknown without establishing an outcome.
        # Keep the stricter empty-packet metric above unchanged.
        result['source_utility_pass'] = bool(expected.issubset(ids)
            and not invalid and not forbidden and provenance
            and (not query.get('conflict') or conflicts)
            and result['required_evidence_present']
            and not result['relevance']['irrelevant'] and not result['relevance']['unlabeled']
            and (not query.get('abstain') or not result['relevance']['answer_useful']))
    return result
