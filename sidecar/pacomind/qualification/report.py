"""Descriptive results by consumer boundary; all declared cases count."""
from collections import Counter, defaultdict
from pathlib import Path
import statistics

from .records import read

OUTCOMES = {'pass', 'fail', 'unsupported', 'setup_error', 'error', 'timeout', 'interrupted', 'not_run'}


def summarize(directory):
    directory = Path(directory)
    run = read(directory / 'run.json')
    rows, groups = [], defaultdict(list)
    for case in run['cases']:
        path = directory / 'attempts' / case['id'] / 'result.json'
        result = read(path) if path.exists() else {'outcome': 'not_run', 'primary_outcome': 'unverified', 'elapsed_ms': None}
        if path.exists() and (result.get('run_id') != run['id'] or result.get('case_sha256') != case['sha256']):
            raise ValueError('Result identity does not match declared case')
        if result['outcome'] not in OUTCOMES:
            raise ValueError('Unknown outcome')
        row = {'case_id': case['id'], 'role': case['role'], 'boundary': case['boundary'],
               'case_sha256': case['sha256'], 'inputs_sha256': case['inputs_sha256'],
               'oracle_sha256': case['oracle_sha256'],
               'evaluator_identity': run.get('evaluator_identities', {}).get(case['evaluator']), **result}
        rows.append(row)
        groups[(case['role'], case['boundary'])].append(row)
    summaries = []
    for (role, boundary), items in sorted(groups.items()):
        durations = {}
        for outcome in sorted({r['outcome'] for r in items}):
            times = [r['elapsed_ms'] for r in items if r['outcome'] == outcome and r.get('elapsed_ms') is not None]
            durations[outcome] = {'samples': len(times), 'median_ms': statistics.median(times) if times else None}
        summaries.append({'role': role, 'boundary': boundary, 'declared': len(items),
            'outcomes': dict(Counter(r['outcome'] for r in items)),
            'primary_passes': sum(r.get('primary_outcome') == 'pass' for r in items),
            'duration_by_outcome': durations,
            'coverage': 'only the listed cases and consumer boundary; not full role qualification'})
    return {'schema': 1, 'kind': 'run', 'run_id': run['id'], 'recipe': run['recipe'],
            'suite_sha256': run['suite_sha256'], 'evidence_mode': run['evidence_mode'],
            'implementation_sha256': run.get('implementation_sha256'),
            'declared': len(rows), 'groups': summaries, 'cases': rows}


def compare(left, right):
    before, after = summarize(left), summarize(right)
    old = {r['case_id']: r for r in before['cases']}
    new = {r['case_id']: r for r in after['cases']}
    rows = []
    for identity in sorted(old.keys() | new.keys()):
        a, b = old.get(identity), new.get(identity)
        comparable = bool(a and b and a['case_sha256'] == b['case_sha256']
                          and a.get('evaluator_identity') is not None
                          and a['evaluator_identity'] == b.get('evaluator_identity')
                          and before['evidence_mode'] == after['evidence_mode'])
        rows.append({'case_id': identity, 'comparable': comparable,
            'grading_changed': bool(a and b and a.get('evaluator_identity') != b.get('evaluator_identity')),
            'before': a['outcome'] if a else 'absent', 'after': b['outcome'] if b else 'absent',
            'primary_before': a.get('primary_outcome') if a else None,
            'primary_after': b.get('primary_outcome') if b else None,
            'elapsed_ms_delta': (b['elapsed_ms'] - a['elapsed_ms']) if comparable
                and a['outcome'] == b['outcome'] == 'pass'
                and a.get('elapsed_ms') is not None and b.get('elapsed_ms') is not None else None})
    return {'schema': 1, 'kind': 'comparison', 'before': before, 'after': after, 'cases': rows,
            'basis': 'recipe comparison; no causal weight, serving identity, tail-latency or confidence claim',
            'system_implementation_changed': before['implementation_sha256'] != after['implementation_sha256'],
            'same_suite': before['suite_sha256'] == after['suite_sha256']}


def markdown(report):
    if report['kind'] == 'comparison':
        lines = ['# Recipe comparison', '', report['basis'], '',
                 '| Case | Comparable | Before | After | Primary before/after | Elapsed change ms |',
                 '| --- | --- | --- | --- | --- | --- |']
        for row in report['cases']:
            lines.append(f"| {row['case_id']} | {row['comparable']} | {row['before']} | {row['after']} | "
                         f"{row['primary_before']}/{row['primary_after']} | {row['elapsed_ms_delta']} |")
    else:
        lines = ['# Model qualification observations', '',
            f"Mode: {report['evidence_mode']}. Declared cases: {report['declared']}.",
            'Coverage is limited to the listed cases and consumer boundaries. Unknown observations remain unknown.', '',
            '| Role | Boundary | All cases | Outcomes | Primary passes | Durations by outcome (median ms, samples) |',
            '| --- | --- | --- | --- | --- | --- |']
        for group in report['groups']:
            lines.append(f"| {group['role']} | {group['boundary']} | {group['declared']} | {group['outcomes']} | "
                f"{group['primary_passes']} | {group['duration_by_outcome']} |")
    return '\n'.join(lines) + '\n'
