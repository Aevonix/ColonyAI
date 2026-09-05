"""The bundled Genesis manifest is the network trust anchor for every colony."""

import json
from pathlib import Path

import pytest

from colony_sidecar.chain import identity


@pytest.fixture(autouse=True)
def _reset_loaded_manifest():
    saved = identity.get_genesis_manifest()
    identity.set_genesis_manifest(None)
    yield
    identity.set_genesis_manifest(saved)


def test_bundled_manifest_ships_inside_the_package():
    path = identity.bundled_genesis_manifest_path()
    assert path is not None
    package_dir = Path(identity.__file__).resolve().parent.parent
    assert path == package_dir / identity.GENESIS_MANIFEST_FILE


def test_bundled_manifest_verifies_against_the_trust_key():
    path = identity.bundled_genesis_manifest_path()
    manifest = json.loads(path.read_text())
    assert manifest["public_key_ed25519"] == identity.GENESIS_TRUST_PUBLIC_KEY
    assert identity.verify_genesis_manifest(manifest) is True
    assert identity.load_genesis_manifest(path) == manifest
    assert identity.get_genesis_manifest() == manifest


def test_resolver_falls_back_to_the_bundled_manifest(tmp_path):
    assert identity.resolve_genesis_manifest_path(tmp_path) == identity.bundled_genesis_manifest_path()


def test_resolver_prefers_the_state_directory_manifest(tmp_path):
    local = tmp_path / identity.GENESIS_MANIFEST_FILE
    local.write_text("{}")
    assert identity.resolve_genesis_manifest_path(tmp_path) == local


def test_a_regular_colony_sees_a_verified_trust_anchor(tmp_path):
    """A colony with its own keys but no local manifest still verifies the anchor."""
    path = identity.resolve_genesis_manifest_path(tmp_path)
    assert identity.load_genesis_manifest(path) is not None
    assert identity.is_genesis("some-other-colony", "00" * 32) is False
