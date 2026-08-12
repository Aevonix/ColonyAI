"""Dispatch admission: file pinning, liveness window, identity probe hook."""

import os
import uuid

import pytest

from colony_hostworker.admission import (
    ADMISSION_MAX_LIFETIME_SECONDS,
    ADMISSION_SCHEMA,
    DispatchAdmission,
    DispatchAdmissionError,
    FileDispatchAdmission,
    sqlite_database_identity,
)
from colony_hostworker.contract import canonical_json_utf8

ORIGIN = "http://127.0.0.1:8123"
TOOLS = ("colony_create_commitment", "colony_research")
NOW = 1_700_000_000.0


class Clock:
    def __init__(self, start=NOW):
        self.value = start

    def __call__(self):
        return self.value


def admission_document(*, binding_identity=None, **overrides):
    document = {
        "schema": ADMISSION_SCHEMA,
        "version": 1,
        "authorized": True,
        "authorization_id": uuid.uuid4().hex,
        "colony_origin": ORIGIN,
        "enabled_tools": sorted(TOOLS),
        "binding_identity": binding_identity,
        "created_at": NOW - 10.0,
        "expires_at": NOW + 3600.0,
    }
    document.update(overrides)
    return document


def write_admission(path, document, mode=0o600, canonical=True):
    if canonical:
        raw = canonical_json_utf8(document) + "\n"
    else:
        import json

        raw = json.dumps(document, indent=2) + "\n"
    path.write_text(raw)
    os.chmod(path, mode)
    return str(path)


def make_admission(tmp_path, *, identity_probe=None, clock=None):
    return FileDispatchAdmission(
        str(tmp_path / "admission.json"),
        colony_origin=ORIGIN,
        enabled_tools=TOOLS,
        clock=clock or Clock(),
        identity_probe=identity_probe,
    )


def test_live_admission_passes(tmp_path):
    admission = make_admission(tmp_path)
    write_admission(tmp_path / "admission.json", admission_document())
    admission.assert_live()
    assert isinstance(admission, DispatchAdmission)


def test_missing_file_refuses(tmp_path):
    admission = make_admission(tmp_path)
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


@pytest.mark.parametrize(
    "overrides",
    [
        {"authorized": False},
        {"authorized": 1},
        {"schema": "SomethingElse"},
        {"version": 2},
        {"authorization_id": "nothex"},
        {"colony_origin": "http://127.0.0.1:9999"},
        {"enabled_tools": sorted(TOOLS)[:1]},
        {"enabled_tools": sorted(TOOLS) + ["colony_task_snooze"]},
        {"expires_at": NOW - 1.0},
        {"created_at": NOW + 120.0},
        {"created_at": NOW - ADMISSION_MAX_LIFETIME_SECONDS * 2,
         "expires_at": NOW + ADMISSION_MAX_LIFETIME_SECONDS},
        {"extra_field": True},
    ],
)
def test_unbound_or_dead_admissions_refuse(tmp_path, overrides):
    admission = make_admission(tmp_path)
    document = admission_document()
    document.update(overrides)
    write_admission(tmp_path / "admission.json", document)
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_missing_field_refuses(tmp_path):
    admission = make_admission(tmp_path)
    document = admission_document()
    del document["expires_at"]
    write_admission(tmp_path / "admission.json", document)
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_non_canonical_bytes_refuse(tmp_path):
    admission = make_admission(tmp_path)
    write_admission(
        tmp_path / "admission.json", admission_document(), canonical=False
    )
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_permissive_mode_refuses(tmp_path):
    admission = make_admission(tmp_path)
    write_admission(tmp_path / "admission.json", admission_document(), mode=0o644)
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_expiry_is_point_of_use(tmp_path):
    clock = Clock()
    admission = make_admission(tmp_path, clock=clock)
    write_admission(tmp_path / "admission.json", admission_document())
    admission.assert_live()
    clock.value = NOW + 7200.0  # beyond expires_at
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


# ---------------------------------------------------------- identity hook


def test_identity_probe_binds_and_passes(tmp_path):
    identity = {"device": 7, "inode": 11, "release": "f" * 40}
    admission = make_admission(tmp_path, identity_probe=lambda: dict(identity))
    write_admission(
        tmp_path / "admission.json",
        admission_document(binding_identity=identity),
    )
    admission.assert_live()


def test_identity_probe_mismatch_in_file_refuses(tmp_path):
    identity = {"device": 7, "inode": 11}
    admission = make_admission(tmp_path, identity_probe=lambda: dict(identity))
    write_admission(
        tmp_path / "admission.json",
        admission_document(binding_identity={"device": 7, "inode": 12}),
    )
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_identity_change_after_construction_refuses(tmp_path):
    identity = {"device": 7, "inode": 11}
    admission = make_admission(tmp_path, identity_probe=lambda: dict(identity))
    write_admission(
        tmp_path / "admission.json",
        admission_document(binding_identity=dict(identity)),
    )
    admission.assert_live()
    identity["inode"] = 999  # the pinned resource was swapped underneath
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_probe_configured_but_file_null_refuses(tmp_path):
    admission = make_admission(tmp_path, identity_probe=lambda: {"device": 1})
    write_admission(
        tmp_path / "admission.json", admission_document(binding_identity=None)
    )
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


def test_no_probe_but_file_identity_refuses(tmp_path):
    admission = make_admission(tmp_path)
    write_admission(
        tmp_path / "admission.json",
        admission_document(binding_identity={"device": 1}),
    )
    with pytest.raises(DispatchAdmissionError):
        admission.assert_live()


# ------------------------------------------------- sqlite identity probe


def test_sqlite_identity_probe_reads_private_file(tmp_path):
    database = tmp_path / "store.sqlite3"
    database.write_bytes(b"")
    os.chmod(database, 0o600)
    identity = sqlite_database_identity(database)
    assert identity["path"] == str(database)
    assert identity["device"] > 0 and identity["inode"] > 0


def test_sqlite_identity_probe_refuses_symlink_and_permissive(tmp_path):
    database = tmp_path / "store.sqlite3"
    database.write_bytes(b"")
    os.chmod(database, 0o644)
    with pytest.raises(DispatchAdmissionError):
        sqlite_database_identity(database)
    os.chmod(database, 0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(database)
    with pytest.raises(DispatchAdmissionError):
        sqlite_database_identity(link)


def test_sqlite_identity_probe_refuses_permissive_wal_sibling(tmp_path):
    database = tmp_path / "store.sqlite3"
    database.write_bytes(b"")
    os.chmod(database, 0o600)
    wal = tmp_path / "store.sqlite3-wal"
    wal.write_bytes(b"")
    os.chmod(wal, 0o644)
    with pytest.raises(DispatchAdmissionError):
        sqlite_database_identity(database)


# --------------------------------------------------------- construction


def test_construction_refuses_bad_configuration(tmp_path):
    with pytest.raises(DispatchAdmissionError):
        FileDispatchAdmission(
            "relative/path.json",
            colony_origin=ORIGIN,
            enabled_tools=TOOLS,
            clock=Clock(),
        )
    with pytest.raises(DispatchAdmissionError):
        FileDispatchAdmission(
            str(tmp_path / "admission.json"),
            colony_origin="http://example.com",
            enabled_tools=TOOLS,
            clock=Clock(),
        )
    with pytest.raises(DispatchAdmissionError):
        FileDispatchAdmission(
            str(tmp_path / "admission.json"),
            colony_origin=ORIGIN,
            enabled_tools=("not_a_governed_tool",),
            clock=Clock(),
        )
    with pytest.raises(DispatchAdmissionError):
        FileDispatchAdmission(
            str(tmp_path / "admission.json"),
            colony_origin=ORIGIN,
            enabled_tools=(),
            clock=Clock(),
        )
