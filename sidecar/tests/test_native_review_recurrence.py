"""A settled native review does not immediately restart an unchanged condition."""
from datetime import datetime, timedelta, timezone
import os
import json

import pytest

from apsimo.initiatives import native_work, store as stores
from apsimo.intelligence.components import initiative_engine as engine_module


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        moment = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment.replace(tzinfo=None)

    for module in (native_work, stores, engine_module):
        monkeypatch.setattr(module, 'datetime', Clock)
    return Clock


def persist(store, candidate, *, retained_old_bucket=False):
    engine_module._apply_recurrence_buckets([candidate])
    row, outcome = store.create_with_outcome(
        type=candidate.type.value, source_type=candidate.type.value,
        created_by='autonomy_loop', action_hint=candidate.action_hint,
        description=candidate.description, entity_id=candidate.entity_id,
        dedup_key=candidate.dedup_key,
        dedup_base=None if retained_old_bucket else candidate.dedup_base,
        context={**candidate.trigger_data, 'candidate_id': candidate.id},
    )
    if retained_old_bucket:
        row = store.update(row.id, dedup_base=candidate.dedup_base)
    return row, outcome


def complete_native(store, row):
    work = native_work.NativeInitiativeWork(store)
    binding = {'native_board': 'default', 'native_task_id': 'task-'+row.id,
               'source_home_id': 'disposable-native-home'}
    work.attach(row.id, 'owner', binding, work.get(row.id)['review']['sha256'])
    result = work.reconcile(row.id, binding, {
        'status': 'done', 'completed_run': True, 'attempt_count': 1,
        'native_run_id': 1, 'summary': 'Reviewed the supplied metadata only.',
        'run_outcome': 'completed',
    })
    assert result['status'] == 'completed'


async def backup_candidate(engine):
    await engine._load_operational_tasks()
    rows = await engine._generate_operational_initiatives()
    return next(row for row in rows if row.entity_id == 'database_backup')


@pytest.fixture
def backup(tmp_path, monkeypatch, clock):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path/'.colony/backups/retained.bak'
    path.parent.mkdir(parents=True)
    path.write_text('disposable old checkpoint')
    stamp = (clock.moment-timedelta(days=27)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.asyncio
async def test_old_proposal_completed_now_suppresses_current_bucket_after_reopen(tmp_path, backup, clock):
    engine = engine_module.InitiativeEngine(None, None, None)
    store = stores.InitiativeStore(tmp_path/'state')
    old_candidate = await backup_candidate(engine)
    old_candidate.action_hint = 'Execute maintenance task'  # Actual retained legacy shape.
    old, _ = persist(store, old_candidate, retained_old_bucket=True)
    original_key = old.dedup_key
    old = store.update(old.id, context={**old.context, 'operator_migration_enrichment': {
        'origin': 'operator_migration_enrichment', 'references': [{'path': '/configured/evidence'}]}})
    clock.moment += timedelta(days=1, minutes=5)
    complete_native(store, old)
    store.close()
    store = stores.InitiativeStore(tmp_path/'state')
    current = await backup_candidate(engine)
    assert current.id != old_candidate.id
    assert current.description != old_candidate.description
    assert current.trigger_data['observed_at'] != old_candidate.trigger_data['observed_at']
    retained, outcome = persist(store, current)
    assert outcome == 'deduped_terminal' and retained.id == old.id
    assert retained.dedup_key == original_key and 'operator_migration_enrichment' in retained.context
    assert store.count() == 1
    store.close()


@pytest.mark.asyncio
async def test_changed_backup_evidence_and_due_interval_rearm_without_active_duplicate(tmp_path, backup, clock):
    engine = engine_module.InitiativeEngine(None, None, None)
    store = stores.InitiativeStore(tmp_path/'state')
    first, _ = persist(store, await backup_candidate(engine))
    complete_native(store, first)
    stamp = backup.stat().st_mtime+86400  # A real changed file timestamp, still stale.
    os.utime(backup, (stamp, stamp))
    changed, outcome = persist(store, await backup_candidate(engine))
    assert outcome == 'created' and changed.id != first.id
    assert persist(store, await backup_candidate(engine))[1] == 'deduped_active'
    clock.moment += timedelta(minutes=20)
    complete_native(store, changed)
    clock.moment += timedelta(hours=12)-timedelta(seconds=1)
    assert persist(store, await backup_candidate(engine))[1] == 'deduped_terminal'
    clock.moment += timedelta(seconds=1)
    due, outcome = persist(store, await backup_candidate(engine))
    assert outcome == 'created' and due.id not in {first.id, changed.id}
    assert store.count() == 3
    store.close()


@pytest.mark.asyncio
async def test_empty_backup_directory_is_a_known_condition_then_file_change(tmp_path, backup, clock):
    backup.unlink()
    engine = engine_module.InitiativeEngine(None, None, None)
    store = stores.InitiativeStore(tmp_path/'state')
    candidate = await backup_candidate(engine)
    assert candidate.trigger_data['latest_file_modified_at'] is None
    first, _ = persist(store, candidate)
    complete_native(store, first)
    assert persist(store, await backup_candidate(engine))[1] == 'deduped_terminal'
    backup.write_text('newly discovered old checkpoint')
    stamp = (clock.moment-timedelta(days=20)).timestamp()
    os.utime(backup, (stamp, stamp))
    assert persist(store, await backup_candidate(engine))[1] == 'created'
    store.close()


@pytest.mark.asyncio
async def test_cancelled_review_retains_existing_periodic_rearm(tmp_path, backup, clock):
    engine = engine_module.InitiativeEngine(None, None, None)
    store = stores.InitiativeStore(tmp_path/'state')
    first, _ = persist(store, await backup_candidate(engine))
    store.cancel(first.id, cancelled_by='owner', reason='Deferred through the existing lifecycle')
    assert persist(store, await backup_candidate(engine))[1] == 'deduped_terminal'
    clock.moment += timedelta(hours=12)
    assert persist(store, await backup_candidate(engine))[1] == 'created'
    store.close()


@pytest.mark.asyncio
async def test_system_condition_uses_existing_predicate_and_six_hour_settlement(tmp_path, clock):
    engine = engine_module.InitiativeEngine(None, None, None)
    store = stores.InitiativeStore(tmp_path/'state')

    async def candidate(status, rate):
        engine._context.pop('system', None)
        engine.add_context('system', [{'entity_id': 'selected-service', 'status': status,
            'error_rate': rate, 'observed_at': clock.moment.isoformat()}])
        return (await engine._generate_system_initiatives())[0]

    first, _ = persist(store, await candidate('down', .2))
    complete_native(store, first)
    clock.moment += timedelta(minutes=5)
    assert persist(store, await candidate('down', .21))[1] == 'deduped_terminal'
    changed, outcome = persist(store, await candidate('degraded', .21))
    assert outcome == 'created'
    complete_native(store, changed)
    clock.moment += timedelta(hours=6)
    assert persist(store, await candidate('degraded', .22))[1] == 'created'
    store.close()


@pytest.mark.asyncio
async def test_receipt_failure_completion_suppresses_same_attempt_but_new_attempt_rearms(tmp_path, clock):
    pointer = tmp_path/'latest-attempt.json'
    attempt = {'schema_version': 1, 'status': 'failed', 'completed_at': clock.moment.isoformat(),
               'captured_at': None, 'receipt_path': None, 'receipt_sha256': None}
    pointer.write_text(json.dumps(attempt))
    engine = engine_module.InitiativeEngine(None, None, None,
        config=engine_module.InitiativeConfig(backup_receipt_path=str(pointer)))
    store = stores.InitiativeStore(tmp_path/'state')
    first, _ = persist(store, await backup_candidate(engine))
    complete_native(store, first)
    clock.moment += timedelta(hours=1)
    # Observation age, UUID, and pointer mtime do not create a second review.
    pointer.write_text(json.dumps(attempt))
    assert persist(store, await backup_candidate(engine))[1] == 'deduped_terminal'
    attempt['completed_at'] = clock.moment.isoformat()
    pointer.write_text(json.dumps(attempt))
    changed, outcome = persist(store, await backup_candidate(engine))
    assert outcome == 'created' and changed.id != first.id
    complete_native(store, changed)
    clock.moment += timedelta(hours=12)
    assert persist(store, await backup_candidate(engine))[1] == 'created'
    store.close()


@pytest.mark.asyncio
async def test_log_growth_does_not_immediately_repeat_settled_native_review(tmp_path, monkeypatch, clock):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path/'.colony/logs/sidecar.log'
    path.parent.mkdir(parents=True)
    with path.open('wb') as stream:
        stream.truncate(101 * 1024 * 1024)
    engine = engine_module.InitiativeEngine(None, None, None)
    store = stores.InitiativeStore(tmp_path/'state')

    async def candidate():
        await engine._load_operational_tasks()
        return (await engine._generate_operational_initiatives())[0]

    first, _ = persist(store, await candidate())
    complete_native(store, first)
    store.close()
    store = stores.InitiativeStore(tmp_path/'state')
    clock.moment += timedelta(minutes=5)
    with path.open('ab') as stream:
        stream.write(b'new ordinary output\n')
    current = await candidate()
    assert current.trigger_data['total_size_bytes'] > first.context['total_size_bytes']
    retained, outcome = persist(store, current)
    assert outcome == 'deduped_terminal' and retained.id == first.id
    clock.moment += timedelta(hours=12)
    assert persist(store, await candidate())[1] == 'created'
    store.close()
