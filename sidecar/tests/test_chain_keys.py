"""Tests for chain.keys backup backends."""

from __future__ import annotations

import json

import pytest

from colony_sidecar.chain.keys import BackupFileShareBackend, KeyShare


def _share(index: int = 1) -> KeyShare:
    return KeyShare(
        colony_id="ab" * 16,
        share_index=index,
        n=3,
        k=2,
        argon2_salt=b"\x01" * 16,
        aes_nonce=b"\x02" * 12,
        ciphertext=b"\x03" * 32,
        tag=b"\x04" * 16,
    )


def test_backup_backend_roundtrip(tmp_path):
    path = tmp_path / "share.colonyshare"
    backend = BackupFileShareBackend(
        file_path=path,
        colony_id="ab" * 16,
        network_id="cd" * 16,
    )
    s = _share(index=2)
    backend.store_share(s)

    # Fresh backend reads the file and verifies checksum.
    reader = BackupFileShareBackend(file_path=path)
    got = reader.retrieve_share(colony_id="ab" * 16, share_index=2)
    assert got is not None
    assert got.share_index == 2
    assert got.ciphertext == s.ciphertext


def test_backup_backend_refuses_conflicting_index(tmp_path):
    path = tmp_path / "share.colonyshare"
    backend = BackupFileShareBackend(
        file_path=path,
        colony_id="ab" * 16,
        network_id="cd" * 16,
    )
    backend.store_share(_share(index=1))

    conflicting = BackupFileShareBackend(
        file_path=path,
        colony_id="ab" * 16,
        network_id="cd" * 16,
    )
    with pytest.raises(ValueError, match="refusing to overwrite"):
        conflicting.store_share(_share(index=2))


def test_backup_backend_overwrite_allowed_when_flag_set(tmp_path):
    path = tmp_path / "share.colonyshare"
    BackupFileShareBackend(
        file_path=path, colony_id="ab" * 16, network_id="cd" * 16
    ).store_share(_share(index=1))

    BackupFileShareBackend(
        file_path=path,
        colony_id="ab" * 16,
        network_id="cd" * 16,
        overwrite=True,
    ).store_share(_share(index=2))

    reader = BackupFileShareBackend(file_path=path)
    assert reader.list_shares("ab" * 16) == [2]


def test_backup_backend_checksum_detects_tampering(tmp_path):
    path = tmp_path / "share.colonyshare"
    BackupFileShareBackend(
        file_path=path, colony_id="ab" * 16, network_id="cd" * 16
    ).store_share(_share(index=1))

    data = json.loads(path.read_text())
    data["share"]["ciphertext"] = "ff" * 32  # Tamper
    path.write_text(json.dumps(data))

    reader = BackupFileShareBackend(file_path=path)
    with pytest.raises(ValueError, match="Checksum mismatch"):
        reader.retrieve_share(colony_id="ab" * 16, share_index=1)


def test_backup_backend_delete_removes_file(tmp_path):
    path = tmp_path / "share.colonyshare"
    backend = BackupFileShareBackend(
        file_path=path, colony_id="ab" * 16, network_id="cd" * 16
    )
    backend.store_share(_share(index=1))
    assert path.exists()

    backend.delete_share(colony_id="ab" * 16, share_index=1)
    assert not path.exists()


def test_backup_backend_requires_metadata_for_new_file(tmp_path):
    path = tmp_path / "share.colonyshare"
    backend = BackupFileShareBackend(file_path=path)
    with pytest.raises(ValueError, match="colony_id and network_id"):
        backend.store_share(_share(index=1))
