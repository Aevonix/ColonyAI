"""Federation trust is explicit; local UUID/key identity needs no trust anchor."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pacomind.chain import identity


@pytest.fixture(autouse=True)
def _reset_loaded_manifest(monkeypatch):
    monkeypatch.delenv("PACOMIND_GENESIS_TRUST_PUBLIC_KEY", raising=False)
    identity.set_genesis_manifest(None)
    yield
    identity.set_genesis_manifest(None)


def test_no_public_manifest_or_fallback(tmp_path):
    package_dir = Path(identity.__file__).resolve().parent.parent
    assert not (package_dir / identity.GENESIS_MANIFEST_FILE).exists()
    assert identity.resolve_genesis_manifest_path(tmp_path) is None


@pytest.fixture
def signed_manifest(tmp_path):
    from pacomind.chain.local_keys import LocalKeyManager
    local_id = identity.get_or_create_pacomind_id(tmp_path)
    keys = LocalKeyManager.generate(keys_dir=tmp_path / "pacomind-keys", pacomind_id=local_id)
    path = tmp_path / identity.GENESIS_MANIFEST_FILE
    manifest = identity.create_genesis_manifest(local_id, keys.public_key_hex(), path,
        private_key_pem=(tmp_path / "pacomind-keys/private.pem").read_bytes())
    return path, manifest


def test_generated_manifest_requires_explicit_key(signed_manifest, monkeypatch):
    path, manifest = signed_manifest
    assert identity.verify_genesis_manifest(manifest) is False
    assert identity.load_genesis_manifest(path) is None
    assert identity.is_genesis(manifest['pacomind_id'], manifest['public_key_ed25519']) is False
    monkeypatch.setenv("PACOMIND_GENESIS_TRUST_PUBLIC_KEY", manifest["public_key_ed25519"])
    assert identity.verify_genesis_manifest(manifest) is True
    assert identity.load_genesis_manifest(path) == manifest
    assert identity.get_genesis_manifest() == manifest
    assert identity.is_genesis(manifest['pacomind_id'], manifest['public_key_ed25519']) is True
    assert identity.is_genesis('another-instance', manifest['public_key_ed25519']) is False


@pytest.mark.parametrize('key', ['', 'invalid-hex', '00' * 32])
def test_invalid_or_different_trust_key_rejects_manifest(signed_manifest, monkeypatch, key):
    path, manifest = signed_manifest
    monkeypatch.setenv("PACOMIND_GENESIS_TRUST_PUBLIC_KEY", key)
    assert identity.load_genesis_manifest(path) is None
    assert identity.get_genesis_manifest() is None


@pytest.mark.parametrize('change', ['tampered', 'unsigned'])
def test_tampered_or_unsigned_manifest_clears_prior_trust(signed_manifest, monkeypatch, change):
    path, manifest = signed_manifest
    monkeypatch.setenv("PACOMIND_GENESIS_TRUST_PUBLIC_KEY", manifest['public_key_ed25519'])
    assert identity.load_genesis_manifest(path) == manifest
    changed = dict(manifest)
    if change == 'tampered':
        changed['pacomind_id'] = 'changed-instance'
    else:
        changed.pop('signature')
    path.write_text(json.dumps(changed))
    assert identity.load_genesis_manifest(path) is None
    assert identity.get_genesis_manifest() is None


def test_removing_config_revokes_cached_trust(signed_manifest, monkeypatch):
    path, manifest = signed_manifest
    monkeypatch.setenv("PACOMIND_GENESIS_TRUST_PUBLIC_KEY", manifest['public_key_ed25519'])
    assert identity.load_genesis_manifest(path) == manifest
    monkeypatch.delenv("PACOMIND_GENESIS_TRUST_PUBLIC_KEY")
    assert identity.get_genesis_manifest() is None
    assert identity.is_genesis(manifest['pacomind_id'], manifest['public_key_ed25519']) is False


def test_creating_manifest_does_not_grant_unconfigured_trust(signed_manifest):
    assert identity.get_genesis_manifest() is None


def test_restoring_identity_does_not_grant_unconfigured_trust(signed_manifest, tmp_path):
    path, manifest = signed_manifest
    backup = identity.backup_pacomind(path.parent)
    restored = tmp_path / 'restored'
    assert identity.restore_pacomind(restored, backup) == manifest['pacomind_id']
    assert (restored / identity.GENESIS_MANIFEST_FILE).is_file()
    assert identity.get_genesis_manifest() is None


def test_resolver_only_reads_the_state_directory_manifest(tmp_path):
    local = tmp_path / identity.GENESIS_MANIFEST_FILE
    local.write_text("{}")
    assert identity.resolve_genesis_manifest_path(tmp_path) == local


@pytest.mark.parametrize('create_manifest', [False, True])
def test_local_cli_identity_initializes_without_federation(tmp_path, monkeypatch, capsys, create_manifest):
    from pacomind import cli
    from pacomind.chain.local_keys import LocalKeyManager
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    monkeypatch.setenv('PACOMIND_STATE_DIR', str(tmp_path))
    cli._cmd_init(SimpleNamespace(encrypt=False, passphrase=None, claim_genesis=create_manifest))
    local_id = identity.get_or_create_pacomind_id(tmp_path)
    keys = LocalKeyManager(tmp_path / 'pacomind-keys', local_id)
    assert len(keys.public_key_hex()) == 64
    assert len(keys.sign(b'local identity remains usable')) == 128
    assert identity.get_or_create_pacomind_id(tmp_path) == local_id
    assert (identity.resolve_genesis_manifest_path(tmp_path) is not None) == create_manifest
    assert identity.get_genesis_manifest() is None
    output = capsys.readouterr().out
    assert 'Genesis claimed' not in output and 'Commit genesis.json' not in output
    if create_manifest:
        assert 'PACOMIND_GENESIS_TRUST_PUBLIC_KEY' in output
