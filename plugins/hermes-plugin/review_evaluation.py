"""Evaluate one explicitly selected native skill proposal using a trusted oracle.

The oracle is operator code, never model-selected code. This module does not
execute the proposed skill, grant permissions, start a worker, or create another
learning store. Hermes' existing pending records, blobs and ledger own recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record(skill, evidence, *, before=None, after=None):
    from tools import skill_ledger as ledger
    entry_id = ledger.append_entry('evaluation', skill, before=before, after=after,
                                   actor='curator', evidence=evidence)
    if not entry_id or ledger.get_entry(entry_id) is None:
        raise RuntimeError('Native evaluation ledger is unavailable')
    with ledger.ledger_path().open('rb') as stream:
        os.fsync(stream.fileno())
    return entry_id


def _measure(oracle, text, phase):
    result = oracle(text, phase=phase)
    # Require actual case results, not a model-authored success sentence.
    rows = result.get('cases') if isinstance(result, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise ValueError('Oracle must return 1..256 explicit cases')
    cases = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'] or row['id'] in cases or type(row.get('passed')) is not bool):
            raise ValueError('Oracle cases require unique IDs and boolean outcomes')
        cases[row['id']] = row['passed']
    encoded = json.dumps(result, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError('Oracle evidence exceeds 64 KiB')
    return json.loads(encoded), cases


def _matches(manifest):
    """Check all native snapshot files before invoking its ordinary rollback."""
    return all(Path(item['path']).is_file()
               and _digest(Path(item['path']).read_bytes()) == item['sha256']
               for item in manifest)


def audit_evaluation(entry_id, oracle, *, oracle_id):
    """Repeat a measured task, reverting only the still-current candidate files."""
    from tools import skill_ledger as ledger, write_approval as approval
    entry = ledger.get_entry(entry_id)
    if not entry or entry.get('action') != 'evaluation' or entry['evidence'].get('status') != 'candidate_passed':
        raise ValueError('Expected a native candidate evaluation entry')
    evidence = entry['evidence']
    if evidence['oracle_id'] != oracle_id:
        raise ValueError('Repeat evaluation requires the same trusted oracle')
    skill = entry['skill']
    if not _matches(entry['after']):
        return {'status': 'changed_elsewhere', 'evaluation_id': entry_id}
    target = Path(evidence['skill_path'])
    try:
        measured, cases = _measure(oracle, target.read_text(), 'post_activation')
        accepted = set(evidence['case_ids']).issubset(cases) and all(cases.values())
    except Exception as error:
        measured = {'error_type': type(error).__name__}
        accepted = False
    # The oracle may take time. Never rewind a later owner edit it overlapped.
    if not _matches(entry['after']):
        return {'status': 'changed_elsewhere', 'evaluation_id': entry_id}
    if accepted:
        status = 'activated'
    else:
        ok, _ = ledger.rollback_entry(entry_id)
        status = 'rolled_back' if ok and _matches(entry['before']) else 'rollback_failed'
    result = {'status': status, 'evaluation_id': entry_id, 'measurement': measured}
    result['result_entry_id'] = _record(skill, result)
    if status in {'activated', 'rolled_back'}:
        approval.discard_pending(approval.SKILLS, evidence['pending_id'])
    return result


def evaluate_pending(pending_id, skill, oracle, *, oracle_id):
    """Compare current/proposed SKILL.md with the same checks before applying.

    Only a main-file edit of this exact existing curator-owned skill is supported.
    Unknown proposals remain in the native pending list for explicit review.
    """
    from tools import skill_ledger as ledger, skill_manager_tool as manager
    from tools import skill_provenance as provenance, skills_tool, write_approval as approval
    from .review import editable_operation
    pending = approval.get_pending(approval.SKILLS, pending_id)
    if not pending or pending.get('origin') != 'background_review':
        raise ValueError('Expected a native background-review proposal')
    payload = pending['payload']
    operation = editable_operation(payload)
    if operation is None or operation.get('name') != skill:
        raise ValueError('Evaluator supports one main-file edit of the explicitly selected skill only')
    token = provenance.set_current_write_origin('background_review')
    try:
        denied = manager._background_review_preflight('edit', skill)
        existing = manager._find_skill(skill)
        if denied or not existing:
            raise ValueError('Selected skill is not available for native curator editing')
        target = existing['path'] / 'SKILL.md'
        original = target.read_bytes()
        original_text = original.decode()
        payload_hash = _digest(json.dumps(payload, sort_keys=True).encode())
        # An interrupted apply retains its proposal and its recoverable native
        # candidate entry. Resume the measurement instead of applying twice.
        entries = ledger.list_entries(skill=skill)
        terminal = {}
        for row in entries:
            evidence = row.get('evidence', {})
            target_id = evidence.get('evaluation_id')
            if row.get('action') == 'evaluation' and evidence.get('status') in {'activated', 'rolled_back'}:
                terminal.setdefault(target_id, evidence['status'])
            elif row.get('action') == 'rollback' and evidence.get('rollback_target'):
                terminal.setdefault(evidence['rollback_target'], 'rolled_back')
        for entry in entries:
            evidence = entry.get('evidence', {})
            if (entry.get('action') == 'evaluation' and evidence.get('status') == 'candidate_passed'
                    and evidence.get('pending_id') == pending_id
                    and evidence.get('payload_sha256') == payload_hash):
                if terminal.get(entry['id']) == 'rolled_back':
                    approval.discard_pending(approval.SKILLS, pending_id)
                    return {'status': 'rolled_back', 'evaluation_id': entry['id'], 'already_final': True}
                if _digest(original) == evidence.get('candidate_sha256'):
                    return audit_evaluation(entry['id'], oracle, oracle_id=oracle_id)
                if terminal.get(entry['id']) == 'activated':
                    return {'status': 'changed_elsewhere', 'evaluation_id': entry['id']}
        if payload.get('_colony_review_base_sha256') != _digest(original):
            return {'status': 'stale_proposal'}
        if operation.get('content'):
            candidate_text = operation['content']
        else:
            occurrences = original_text.count(operation['old_string'])
            if not occurrences or (occurrences != 1 and not operation.get('replace_all', False)):
                return {'status': 'patch_conflict'}
            candidate_text = original_text.replace(operation['old_string'], operation['new_string'],
                                                   -1 if operation.get('replace_all', False) else 1)
        candidate = candidate_text.encode()
        baseline, old_cases = _measure(oracle, original_text, 'baseline')
        proposed, new_cases = _measure(oracle, candidate_text, 'candidate')
        evidence = {'pending_id': pending_id, 'payload_sha256': payload_hash,
                    'oracle_id': oracle_id, 'skill_path': str(target),
                    'before_sha256': _digest(original), 'candidate_sha256': _digest(candidate),
                    'baseline': baseline, 'candidate': proposed, 'case_ids': list(old_cases)}
        improved = (original != candidate and old_cases.keys() == new_cases.keys() and all(new_cases.values())
                    and sum(new_cases.values()) > sum(old_cases.values()))
        if not improved:
            return {'status': 'not_improved', 'result_entry_id': _record(skill, {**evidence, 'status': 'not_improved'})}
        if target.read_bytes() != original or approval.get_pending(approval.SKILLS, pending_id) != pending:
            return {'status': 'changed_elsewhere'}
        before = ledger.snapshot_paths(existing['path'])
        if not any(item['path'] == str(target) and item['sha256'] == _digest(original) for item in before):
            return {'status': 'changed_elsewhere'}
        after = [dict(item) for item in before]
        for item in after:
            if item['path'] == str(target):
                item['sha256'] = ledger._store_blob(candidate)
        # This is explicitly candidate evidence, not a claim that a mutation
        # already happened. It is also a native rollback target if apply dies
        # before Hermes' best-effort mutation telemetry is written.
        entry_id = _record(skill, {**evidence, 'status': 'candidate_passed'}, before=before, after=after)
        if not _matches(before):
            return {'status': 'changed_elsewhere', 'evaluation_id': entry_id}
        viewed = json.loads(skills_tool.skill_view(skill, preprocess=False))
        if not viewed.get('success', True):
            raise RuntimeError('Native current-content read failed')
        if not _matches(before):
            return {'status': 'changed_elsewhere', 'evaluation_id': entry_id}
        applied = json.loads(manager.apply_skill_pending(payload))
        if not applied.get('success'):
            return {'status': 'apply_failed', 'evaluation_id': entry_id}
        return audit_evaluation(entry_id, oracle, oracle_id=oracle_id)
    finally:
        provenance.reset_current_write_origin(token)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--oracle', required=True, help='Trusted local module:function; must bound its own task calls')
    parser.add_argument('--skill', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--pending')
    group.add_argument('--audit', help='Native candidate evaluation entry ID')
    args = parser.parse_args(argv)
    module, name = args.oracle.split(':', 1)
    loaded = importlib.import_module(module)
    oracle = getattr(loaded, name)
    oracle_id = args.oracle + ':' + _digest(Path(loaded.__file__).read_bytes())
    if args.audit:
        from tools import skill_ledger
        entry = skill_ledger.get_entry(args.audit)
        if not entry or entry.get('skill') != args.skill:
            raise ValueError('Evaluation does not belong to the selected skill')
        result = audit_evaluation(args.audit, oracle, oracle_id=oracle_id)
    else:
        result = evaluate_pending(args.pending, args.skill, oracle, oracle_id=oracle_id)
    print(json.dumps(result))
    return 0 if result['status'] in {'activated', 'rolled_back', 'not_improved'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
