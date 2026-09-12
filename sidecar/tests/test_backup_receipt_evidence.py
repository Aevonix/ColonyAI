"""Configured capture evidence replaces legacy file-age guesses, not health checks."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest

from apsimo.initiatives.backup_evidence import backup_review_task
from apsimo.initiatives.native_work import NativeInitiativeWork
from apsimo.initiatives.store import InitiativeStore
from apsimo.autonomy.config import AutonomyConfig
from apsimo.intelligence.components.initiative_engine import InitiativeConfig, InitiativeEngine, InitiativeType


def publish(path, captured):
    receipt = path.parent/'run/receipt.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({'status': 'captured', 'at': captured.isoformat(),
                      'coverage': ['Capture only; restoration not tested.']}).encode()
    receipt.write_bytes(raw)
    pointer = {'schema_version': 1, 'status': 'captured',
               'completed_at': captured.isoformat(), 'captured_at': captured.isoformat(),
               'receipt_path': 'run/receipt.json', 'receipt_sha256': hashlib.sha256(raw).hexdigest()}
    path.write_text(json.dumps(pointer))
    return pointer, receipt


@pytest.mark.asyncio
async def test_actual_loader_replaces_legacy_check_and_keeps_other_operational_checks(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    old = tmp_path/'.colony/backups/old.bak'
    old.parent.mkdir(parents=True)
    old.write_text('unverified old legacy file')
    stamp = (datetime.now(timezone.utc)-timedelta(days=27)).timestamp()
    os.utime(old, (stamp, stamp))
    engine = InitiativeEngine(None, None, None)
    await engine._load_operational_tasks()
    assert engine._context['operational_tasks'][0]['evidence_scope'] == 'legacy_bak_directory_only'
    pointer = tmp_path/'latest-attempt.json'
    publish(pointer, datetime.now(timezone.utc)-timedelta(hours=1))
    monkeypatch.setenv('COLONY_INITIATIVE_BACKUP_RECEIPT', str(pointer))
    log = tmp_path/'.colony/logs/large.log'
    monkeypatch.setenv('APSIMO_LOG_PATH', str(log))
    log.parent.mkdir(parents=True)
    with log.open('wb') as stream:
        stream.truncate(101*1024*1024)
    config = InitiativeConfig.from_env()
    assert config.backup_receipt_path == str(pointer)
    selected = InitiativeEngine(None, None, None, config=config)
    await selected._load_operational_tasks()
    assert [task['entity_id'] for task in selected._context['operational_tasks']] == ['log_rotation']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['stale', 'failed', 'unavailable'])
async def test_receipt_problem_reaches_native_review_through_normal_generation(tmp_path, status):
    now = datetime.now(timezone.utc)
    pointer = tmp_path/'latest-attempt.json'
    value, receipt = publish(pointer, now-timedelta(days=8))
    # File timestamps are current. Capture freshness must come from the receipt.
    assert (now.timestamp()-receipt.stat().st_mtime) < 10
    if status == 'failed':
        value.update(status='failed', completed_at=now.isoformat(),
                     captured_at=None, receipt_path=None, receipt_sha256=None)
        pointer.write_text(json.dumps(value))
    elif status == 'unavailable':
        pointer.unlink()
    engine = InitiativeEngine(None, None, None, config=InitiativeConfig(backup_receipt_path=str(pointer)))
    # Use the real operational loader and normal filtering, with unrelated
    # graph/relationship loads excluded from this disposable receipt fixture.
    engine._load_graph_context = engine._load_operational_tasks
    candidate, = await engine.generate(types=[InitiativeType.OPERATIONAL],
        min_priority=AutonomyConfig().initiative_confidence_threshold)
    store = InitiativeStore(tmp_path/'state')
    try:
        row = store.create(type='operational', source_type='operational', created_by='autonomy_loop',
                           action_hint=candidate.action_hint, description=candidate.description,
                           entity_id=candidate.entity_id, context=candidate.trigger_data)
        review = NativeInitiativeWork(store).get(row.id)['review']
        assert review['action'] == 'operational_review'
        assert 'configured_backup_receipt' in review['body'] and str(pointer) in review['body']
        assert 'legacy .bak' not in candidate.description
        assert candidate.trigger_data['receipt_status'] == ('captured' if status == 'stale' else status)
        assert candidate.trigger_data.get('age_days', 0) != 999
        if status == 'stale':
            assert candidate.trigger_data['receipt_sha256'] == value['receipt_sha256']
            assert str(receipt) in review['body'] and 'unverified' in candidate.description
        elif status == 'failed':
            assert 'failed' in candidate.description and 'age_days' not in candidate.trigger_data
        else:
            assert 'unknown' in candidate.description and 'age_days' not in candidate.trigger_data
    finally:
        store.close()


@pytest.mark.parametrize('problem', ['missing', 'malformed', 'hash_mismatch', 'future', 'naive', 'wrong_status', 'incomplete_failure'])
def test_unavailable_receipt_is_unknown_without_legacy_fallback(tmp_path, problem):
    now = datetime.now(timezone.utc)
    pointer = tmp_path/'latest-attempt.json'
    value, receipt = publish(pointer, now-timedelta(hours=1))
    if problem == 'missing':
        pointer.unlink()
    elif problem == 'malformed':
        pointer.write_text('{')
    elif problem == 'hash_mismatch':
        receipt.write_text('{}')
    else:
        if problem == 'future':
            value['completed_at'] = (now+timedelta(days=1)).isoformat()
        elif problem == 'naive':
            value['completed_at'] = now.replace(tzinfo=None).isoformat()
        elif problem == 'incomplete_failure':
            value = {'schema_version': 1, 'status': 'failed', 'completed_at': now.isoformat()}
        else:
            value['status'] = 'healthy'
        pointer.write_text(json.dumps(value))
    task = backup_review_task(str(pointer), now)
    assert task['receipt_status'] == 'unavailable'
    assert task['receipt_unavailable_reason'] == ('missing' if problem == 'missing' else 'invalid')
    assert 'unknown' in task['description'] and 'age_days' not in task
    assert task['captured_at'] is None and task['receipt_sha256'] is None


def test_seven_day_due_boundary_uses_capture_time_not_pointer_republication(tmp_path):
    now = datetime.now(timezone.utc)
    pointer = tmp_path/'latest-attempt.json'
    publish(pointer, now-timedelta(days=7))
    assert backup_review_task(str(pointer), now) is None
    assert backup_review_task(str(pointer), now+timedelta(seconds=1))['receipt_status'] == 'captured'
