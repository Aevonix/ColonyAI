"""Canonical source images must survive backup as evidence, not just captions."""
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from colony_sidecar import backup
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.media import SourceMedia
from test_source_media import image_bytes, message


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
