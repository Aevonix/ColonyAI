"""Canonical source images must survive backup as evidence, not just captions."""
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from apsimo import backup
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.media import SourceMedia
from test_source_media import image_bytes, message
from test_hermes_turn_outbox import _load_client


@pytest.fixture
def evidence(tmp_path):
    state = tmp_path / 'state'
    ledger = TurnIdempotencyLedger(state / 'turn-idempotency.db')
    ledger.record_source('image-source', contact_id='fixture-contact', session_id='original',
                         messages=[message()], derive_claims=False)
    media = SourceMedia(ledger)
    asset = hashlib.sha256(image_bytes()).hexdigest()
    job = media.claim_job()
    assert media.finish(job, description='A blue circle beside a red rectangle.', model='fixture-vision')
    return state, ledger, media, asset


def test_real_image_source_caption_scope_and_forgetting_survive_restore(evidence, tmp_path):
    state, ledger, media, asset = evidence
    # This unrelated orphan is not owned by the captured ledger.
    (media.store._originals_dir / ('0' * 64 + '.png')).write_bytes(b'unowned')
    archive = backup.create_full_backup(state, tmp_path / 'archives',
                                       include_graph=False, include_vectors=False)
    destination = tmp_path / 'restored'
    result = backup.restore_full_backup(archive, destination)
    assert result['source_images'] == 1
    restored = SourceMedia(TurnIdempotencyLedger(destination / 'turn-idempotency.db'))
    assert restored.read(asset, contact_id='fixture-contact', session_id='later')[0] == image_bytes()
    assert restored.search('blue circle', contact_id='fixture-contact', session_id='later')[0]['asset_id'] == 'sha256:' + asset
    with pytest.raises(KeyError):
        restored.read(asset, contact_id='foreign-contact', session_id='later')
    assert [p.name for p in restored.store._originals_dir.iterdir()] == [asset + '.png']
    assert restored.store._original_path(asset, 'image/png').stat().st_mode & 0o777 == 0o600
    assert restored.ledger.erase_sources(contact_id='fixture-contact', turn_ids=['image-source'])['media_cleanup'] == 'complete'
    assert not restored.store._original_path(asset, 'image/png').exists()
    assert restored.search('blue circle', contact_id='fixture-contact', session_id='later') == []


@pytest.mark.parametrize('failure', ['missing', 'corrupt'])
def test_backup_never_publishes_missing_or_corrupt_original(evidence, tmp_path, failure):
    state, _, media, asset = evidence
    original = media.store._original_path(asset, 'image/png')
    if failure == 'missing':
        original.unlink()
    else:
        original.write_bytes(b'not-the-original')
    with pytest.raises((RuntimeError, ValueError), match='source image'):
        backup.create_full_backup(state, tmp_path / 'archives', include_graph=False, include_vectors=False)
    assert not list((tmp_path / 'archives').iterdir())


@pytest.mark.parametrize('archive_version', [1, backup.BACKUP_VERSION])
def test_restore_preflights_originals_before_overwriting_destination(evidence, tmp_path, archive_version):
    state, _, _, asset = evidence
    archive = backup.create_full_backup(state, tmp_path / 'archives', include_graph=False, include_vectors=False)
    unpacked = tmp_path / 'unpacked'; unpacked.mkdir()
    backup._extract_archive(archive, unpacked)
    root = backup._find_backup_root(unpacked)
    (root / 'images' / 'sources' / 'originals' / (asset + '.png')).unlink()
    metadata = json.loads((root / 'meta.json').read_text())
    metadata['backup_version'] = archive_version
    (root / 'meta.json').write_text(json.dumps(metadata))
    broken = tmp_path / 'missing-original.tar.gz'
    backup._create_archive(root, broken)
    destination = tmp_path / 'restored'; destination.mkdir()
    existing = destination / 'turn-idempotency.db'; existing.write_bytes(b'existing-state')
    with pytest.raises(ValueError, match='missing an original source image'):
        backup.restore_full_backup(broken, destination)
    assert existing.read_bytes() == b'existing-state'
    assert list(destination.iterdir()) == [existing]


def test_vacuum_failure_uses_consistent_backup_including_committed_wal(tmp_path, monkeypatch):
    state = tmp_path / 'state'; state.mkdir()
    source_path = state / 'fixture.db'
    connection = sqlite3.connect(source_path)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE facts (value TEXT)')
        connection.execute("INSERT INTO facts VALUES ('committed-in-wal')")
        connection.commit()
        assert source_path.with_name('fixture.db-wal').stat().st_size > 0
        original_connect = sqlite3.connect
        class NoVacuum(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith('VACUUM INTO'):
                    raise sqlite3.OperationalError('controlled VACUUM failure')
                return super().execute(sql, *args, **kwargs)
        monkeypatch.setattr(backup.sqlite3, 'connect', lambda *a, **kw: original_connect(*a, **kw, factory=NoVacuum))
        snapshots = tmp_path / 'snapshots'
        records = backup._snapshot_databases(state, snapshots)
        assert records[0]['method'] == 'sqlite_backup'
        restored = original_connect(snapshots / 'fixture.db')
        try:
            assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert restored.execute('SELECT value FROM facts').fetchone()[0] == 'committed-in-wal'
        finally:
            restored.close()
    finally:
        connection.close()


def test_restore_replaces_crashed_destination_wal_with_backed_up_state(evidence, tmp_path):
    state, _, _, asset = evidence
    archive = backup.create_full_backup(state, tmp_path / 'archives', include_graph=False, include_vectors=False)
    destination = tmp_path / 'restored'; destination.mkdir()
    db = destination / 'turn-idempotency.db'
    subprocess.run([sys.executable, '-c', '''
import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE obsolete(value TEXT)')
c.execute("INSERT INTO obsolete VALUES ('old-target')")
c.commit()
os._exit(0)
''', str(db)], check=True)
    assert db.with_name(db.name + '-wal').stat().st_size > 0
    backup.restore_full_backup(archive, destination)
    restored = SourceMedia(TurnIdempotencyLedger(db))
    assert restored.read(asset, contact_id='fixture-contact', session_id='new')[0] == image_bytes()
    with sqlite3.connect(db) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='obsolete'").fetchone()
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def _memory_archive(state, output):
    (state / 'colony-id').write_text('recovery-fixture-colony')
    return backup.create_full_backup(state, output, include_graph=False, include_vectors=False)


def test_memory_salvage_keeps_newer_erasures_and_offline_host_cursor(evidence, tmp_path):
    state, ledger, media, asset = evidence
    client = _load_client('recovery_offline_client')
    outbox = client.TurnOutbox(tmp_path / 'offline-host' / 'outbox.sqlite3')
    outbox.enqueue('image-source', {'turn_id': 'image-source', 'contact_id': 'fixture-contact',
        'session_id': 'original', 'checkpoint_messages': [message()]})
    archive = _memory_archive(state, tmp_path / 'archives')
    assert ledger.erase_sources(contact_id='fixture-contact', turn_ids=['image-source'])['media_cleanup'] == 'complete'
    page = ledger.erasure_feed('fixture-contact')
    outbox.apply_erasure_page('fixture-contact', page)
    cursor = outbox.erasure_watermark('fixture-contact')
    assert cursor > 0

    old = tmp_path / 'old-restore'
    backup.restore_full_backup(archive, old)
    old_media = SourceMedia(TurnIdempotencyLedger(old / 'turn-idempotency.db'))
    assert old_media.read(asset, contact_id='fixture-contact', session_id='later')[0] == image_bytes()
    with pytest.raises(ValueError, match='restore requires reconciliation'):
        old_media.ledger.erasure_feed('fixture-contact', after=cursor)

    destination = tmp_path / 'current-memory'
    summary = backup.restore_source_memory(archive, destination, current_state=state)
    recovered = SourceMedia(TurnIdempotencyLedger(destination / 'turn-idempotency.db'))
    assert recovered.ledger.erasure_feed('fixture-contact', after=cursor)['complete']
    assert recovered.search('blue circle', contact_id='fixture-contact', session_id='later') == []
    with pytest.raises(KeyError):
        recovered.read(asset, contact_id='fixture-contact', session_id='later')
    assert not (destination / 'images').exists()
    assert summary['erasure_head'] == cursor
    assert summary['source_images'] == 0
    assert summary['runtime_authority_restored'] is False
    with sqlite3.connect(outbox.path) as db:
        assert db.execute('SELECT count(*) FROM turn_outbox').fetchone()[0] == 0


@pytest.mark.parametrize('lost_original', ['missing', 'corrupt'])
def test_memory_salvage_recovers_owned_bytes_and_current_corrections_scope_only(evidence, tmp_path, lost_original):
    state, ledger, media, asset = evidence
    from apsimo.turns.idempotency import canonical_turn_digest
    source = [{'role': 'user', 'content': 'The toolbox is in the study.'}]
    ledger.record_source('text-source', contact_id='fixture-contact', session_id='original',
                         messages=source, derive_claims=False)
    runtime_files = ('task_queue.db', 'approval_authority.db', 'contacts.db', 'colony-action-journal.db')
    for filename in runtime_files:
        with sqlite3.connect(state / filename) as db:
            db.execute('CREATE TABLE records(id TEXT)')
            db.execute("INSERT INTO records VALUES ('old-runtime-state')")
    (state / '.env').write_text('EXAMPLE_SETTING=old\n')
    archive = _memory_archive(state, tmp_path / 'archives')
    for filename in runtime_files:
        with sqlite3.connect(state / filename) as db:
            db.execute("UPDATE records SET id='current-runtime-state'")
    current_runtime_hashes = {name: hashlib.sha256((state / name).read_bytes()).hexdigest()
                              for name in runtime_files}

    annotation = ledger.append_source_annotation(
        contact_id='fixture-contact', session_id='later', annotation_id='correct-location',
        source_id='text-source', source_version=canonical_turn_digest(source), excerpt='toolbox',
        correction='This was an unverified report, not a confirmed location.', author_principal='fixture-owner')
    ledger.record_source('session-only', contact_id='fixture-contact', session_id='private-session',
        scope='session', messages=[{'role': 'user', 'content': 'Temporary workshop arrangement.'}], derive_claims=False)
    original = media.store._original_path(asset, 'image/png')
    if lost_original == 'missing':
        original.unlink()
    else:
        original.write_bytes(b'corrupted original')

    destination = tmp_path / 'memory-bundle'
    summary = backup.restore_source_memory(archive, destination, current_state=state)
    assert current_runtime_hashes == {name: hashlib.sha256((state / name).read_bytes()).hexdigest()
                                      for name in runtime_files}
    assert set(path.name for path in destination.iterdir()) == {
        'turn-idempotency.db', 'images', 'source-memory-recovery.json'}
    recovered_ledger = TurnIdempotencyLedger(destination / 'turn-idempotency.db')
    recovered = SourceMedia(recovered_ledger)
    assert recovered.read(asset, contact_id='fixture-contact', session_id='later')[0] == image_bytes()
    assert recovered.search('blue circle', contact_id='fixture-contact', session_id='later')
    with pytest.raises(KeyError):
        recovered.read(asset, contact_id='foreign-fixture', session_id='later')
    assert recovered_ledger.search_sources('workshop', contact_id='fixture-contact', session_id='later') == []
    assert recovered_ledger.search_sources('workshop', contact_id='fixture-contact', session_id='private-session')
    with sqlite3.connect(destination / 'turn-idempotency.db') as db:
        assert db.execute('SELECT target_source_id FROM source_annotations WHERE annotation_source_id=?',
                          (annotation['source_id'],)).fetchone()[0] == 'text-source'
    assert summary['images_recovered_from_archive'] == 1
    assert summary['serving_admitted'] is False
    assert json.loads((destination / 'source-memory-recovery.json').read_text()) == summary
    assert recovered_ledger.erase_sources(contact_id='fixture-contact', turn_ids=['image-source'])['media_cleanup'] == 'complete'


@pytest.mark.parametrize('missing', ['colony-id', 'turn-idempotency.db'])
def test_memory_salvage_requires_surviving_identity_and_history(evidence, tmp_path, missing):
    state, _, _, _ = evidence
    archive = _memory_archive(state, tmp_path / 'archives')
    (state / missing).unlink()
    destination = tmp_path / 'absent'
    with pytest.raises(ValueError, match='surviving colony identity and source ledger'):
        backup.restore_source_memory(archive, destination, current_state=state)
    assert not destination.exists()


def test_memory_salvage_rejects_older_surviving_erasure_history(evidence, tmp_path):
    state, ledger, _, _ = evidence
    original = _memory_archive(state, tmp_path / 'first')
    stale = tmp_path / 'stale-current'
    backup.restore_full_backup(original, stale)
    ledger.erase_sources(contact_id='fixture-contact', turn_ids=['image-source'])
    newer_archive = _memory_archive(state, tmp_path / 'second')
    destination = tmp_path / 'absent'
    with pytest.raises(ValueError, match='erasure history does not extend'):
        backup.restore_source_memory(newer_archive, destination, current_state=stale)
    assert not destination.exists()


def test_memory_salvage_rejects_different_history_at_the_same_erasure_head(evidence, tmp_path):
    state, ledger, _, _ = evidence
    ledger.erase_sources(contact_id='fixture-contact', turn_ids=['image-source'])
    archive = _memory_archive(state, tmp_path / 'archives')
    other = tmp_path / 'different-current'
    alternate = TurnIdempotencyLedger(other / 'turn-idempotency.db')
    (other / 'colony-id').write_text('recovery-fixture-colony')
    alternate.record_source('different-source', contact_id='fixture-contact', session_id='other',
        messages=[{'role': 'user', 'content': 'Different history.'}], derive_claims=False)
    alternate.erase_sources(contact_id='fixture-contact', turn_ids=['different-source'])
    assert alternate.erasure_watermark('fixture-contact') == ledger.erasure_watermark('fixture-contact')
    with pytest.raises(ValueError, match='erasure history does not extend'):
        backup.restore_source_memory(archive, tmp_path / 'absent', current_state=other)


def test_memory_salvage_uses_existing_encrypted_archive_support(evidence, tmp_path):
    state, _, _, asset = evidence
    (state / 'colony-id').write_text('recovery-fixture-colony')
    archive = backup.create_full_backup(state, tmp_path / 'archives',
        passphrase=b'fixture-passphrase', include_graph=False, include_vectors=False)
    destination = tmp_path / 'memory'
    with pytest.raises(ValueError, match='passphrase required'):
        backup.restore_source_memory(archive, destination, current_state=state)
    assert not destination.exists()
    backup.restore_source_memory(archive, destination, current_state=state,
                                 passphrase=b'fixture-passphrase')
    recovered = SourceMedia(TurnIdempotencyLedger(destination / 'turn-idempotency.db'))
    assert recovered.read(asset, contact_id='fixture-contact', session_id='later')[0] == image_bytes()


def test_memory_salvage_rejects_identity_mismatch_and_existing_destination(evidence, tmp_path):
    state, _, _, _ = evidence
    archive = _memory_archive(state, tmp_path / 'archives')
    destination = tmp_path / 'absent'
    (state / 'colony-id').write_text('unrelated-colony')
    with pytest.raises(ValueError, match='different colony identities'):
        backup.restore_source_memory(archive, destination, current_state=state)
    assert not destination.exists()
    (state / 'colony-id').write_text('recovery-fixture-colony')
    with pytest.raises(ValueError, match='fresh destination'):
        backup.restore_source_memory(archive, state, current_state=state)


def test_memory_salvage_checks_owned_originals_before_publication(evidence, tmp_path):
    state, _, media, asset = evidence
    archive = _memory_archive(state, tmp_path / 'archives')
    unpacked = tmp_path / 'unpacked'; unpacked.mkdir()
    backup._extract_archive(archive, unpacked)
    root = backup._find_backup_root(unpacked)
    (root / 'images' / 'sources' / 'originals' / (asset + '.png')).write_bytes(b'bad original')
    broken = tmp_path / 'broken.tar.gz'
    backup._create_archive(root, broken)
    media.store._original_path(asset, 'image/png').unlink()
    destination = tmp_path / 'absent'
    with pytest.raises(ValueError, match='no matching original'):
        backup.restore_source_memory(broken, destination, current_state=state)
    assert not destination.exists()


def test_restore_cli_reports_scope_and_requires_explicit_memory_destination(evidence, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from apsimo import cli
    state, _, _, _ = evidence
    archive = _memory_archive(state, tmp_path / 'archives')
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    args = SimpleNamespace(full=False, memory_only=True, input=str(archive), passphrase=None,
                           current_state=str(state), output=None, force_identity=False)
    with pytest.raises(SystemExit) as stopped:
        cli._cmd_restore(args)
    assert stopped.value.code == 2
    args.output = str(tmp_path / 'memory')
    monkeypatch.setattr(sys, 'argv', ['colony', 'restore', '--memory-only', '--input', str(archive),
                                     '--current-state', str(state), '--output', args.output])
    cli.main()
    output = capsys.readouterr().out
    assert 'Current source memory recovered' in output
    assert 'separately current runtime bindings' in output
    assert "Run 'colony start'" not in output

    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path / 'full-state'))
    args.full = True; args.memory_only = False; args.current_state = None; args.output = None
    cli._cmd_restore(args)
    output = capsys.readouterr().out
    assert 'Archive reconstructed' in output
    assert 'Reconcile current authority, erasures and completed effects' in output
    assert "Run 'colony start'" not in output
