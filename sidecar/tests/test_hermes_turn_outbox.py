"""Durable canonical turn-writer contract for one-shot Hermes processes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"
CLIENT_PATH = PLUGIN_DIR / "client.py"


def _load_client(name="colony_hermes_turn_outbox_client_test"):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_plugin(name="colony_hermes_turn_outbox_plugin_test"):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _payload(assistant="delivered reply"):
    return {
        "session_id": "session-1",
        "contact_id": "cid-owner",
        "turn_id": "turn-1",
        "user_message": "hello",
        "assistant_message": assistant,
        "summary": "summary",
        "model": "model-a",
        "sender": {"platform": "sms", "user_id": "+15550001"},
    }


_PREDECESSOR_SCHEMA = """
    CREATE TABLE IF NOT EXISTS turn_outbox (
        turn_id TEXT PRIMARY KEY,
        envelope_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'delivered')),
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_error TEXT NOT NULL DEFAULT ''
    )
"""

_CURRENT_SCHEMA = """
    CREATE TABLE turn_outbox (
        turn_id TEXT PRIMARY KEY,
        envelope_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'delivered')),
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_error TEXT NOT NULL DEFAULT '',
        lease_id TEXT NOT NULL DEFAULT '',
        lease_expires_at REAL NOT NULL DEFAULT 0
    )
"""

_PENDING_INDEX = (
    "CREATE INDEX turn_outbox_pending_idx "
    "ON turn_outbox(state, lease_expires_at, created_at, turn_id)"
)
_APPLICATION_ID = 1_129_270_361  # big-endian ASCII ``COLY``
_USER_VERSION = 2


def _create_database(path: Path, statements: list[str], *, application_id=0,
                     user_version=0) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={int(application_id)}")
        connection.execute(f"PRAGMA user_version={int(user_version)}")
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _logical_database_snapshot(path: Path) -> tuple:
    connection = sqlite3.connect(path)
    try:
        objects = tuple(connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall())
        tables = tuple(
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        )
        rows = []
        for table in tables:
            escaped = table.replace('"', '""')
            rows.append((table, tuple(connection.execute(
                f'SELECT * FROM "{escaped}" ORDER BY rowid'
            ).fetchall())))
        return (
            objects,
            tuple(rows),
            int(connection.execute("PRAGMA application_id").fetchone()[0]),
            int(connection.execute("PRAGMA user_version").fetchone()[0]),
        )
    finally:
        connection.close()


def test_private_outbox_accepts_new_and_existing_owner_private_files(tmp_path):
    module = _load_client("colony_hermes_private_outbox_accept_test")
    database = tmp_path / "turn-outbox.sqlite3"
    outbox = module.TurnOutbox(database)

    first = outbox.prepare()
    second = module.TurnOutbox(database).prepare()

    assert first["configuration_ready"] is True
    assert first["physical_power_loss_verified"] is False
    assert first == second
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert database.stat().st_uid == os.geteuid()
    assert database.stat().st_nlink == 1
    assert first["application_id"] == _APPLICATION_ID
    assert first["user_version"] == _USER_VERSION
    assert first["fullfsync"] == "ON"
    assert first["checkpoint_fullfsync"] == "ON"

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == (
            _APPLICATION_ID
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            _USER_VERSION
        )
        indexes = connection.execute("PRAGMA index_list(turn_outbox)").fetchall()
        pending = next(row for row in indexes if row[1] == "turn_outbox_pending_idx")
        assert pending[2:] == (0, "c", 0)
        assert [row[2] for row in connection.execute(
            "PRAGMA index_xinfo(turn_outbox_pending_idx)"
        ).fetchall() if row[5]] == [
            "state", "lease_expires_at", "created_at", "turn_id",
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO turn_outbox VALUES "
                "('invalid','d','{}','not-a-state',0,0,0,'','',0)"
            )
    finally:
        connection.close()


def test_exact_predecessor_migrates_transactionally_and_preserves_rows(tmp_path):
    module = _load_client("colony_hermes_exact_predecessor_migration_test")
    database = tmp_path / "turn-outbox.sqlite3"
    _create_database(database, [
        _PREDECESSOR_SCHEMA,
        """
        INSERT INTO turn_outbox (
            turn_id, envelope_sha256, payload_json, state, attempts,
            created_at, updated_at, last_error
        ) VALUES ('turn-old', 'digest-old', '{}', 'pending', 2, 1, 2, 'old')
        """,
    ])

    ready = module.TurnOutbox(database).prepare()

    assert ready["configuration_ready"] is True
    connection = sqlite3.connect(database)
    try:
        columns = connection.execute("PRAGMA table_info(turn_outbox)").fetchall()
        assert [row[1] for row in columns] == [
            "turn_id", "envelope_sha256", "payload_json", "state", "attempts",
            "created_at", "updated_at", "last_error", "lease_id",
            "lease_expires_at",
        ]
        assert connection.execute(
            "SELECT turn_id, attempts, last_error, lease_id, lease_expires_at "
            "FROM turn_outbox"
        ).fetchall() == [("turn-old", 2, "old", "", 0.0)]
        assert connection.execute("PRAGMA application_id").fetchone()[0] == (
            _APPLICATION_ID
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            _USER_VERSION
        )
    finally:
        connection.close()


@pytest.mark.parametrize("malformation", [
    "partial_schema",
    "broken_check",
    "unique_pending_index",
    "wrong_pending_index_order",
    "unexpected_index",
    "unexpected_trigger",
    "unexpected_foreign_key",
    "wrong_application_id",
    "wrong_user_version",
])
def test_malformed_or_unknown_schema_is_rejected_without_mutation(
    tmp_path, malformation,
):
    module = _load_client(f"colony_hermes_malformed_{malformation}_test")
    database = tmp_path / "turn-outbox.sqlite3"
    current = _CURRENT_SCHEMA
    statements: list[str]
    application_id = _APPLICATION_ID
    user_version = _USER_VERSION
    if malformation == "partial_schema":
        statements = ["CREATE TABLE turn_outbox (turn_id TEXT PRIMARY KEY)"]
    elif malformation == "broken_check":
        statements = [
            current.replace(
                "state IN ('pending', 'delivered')",
                "state IN ('pending', 'delivered', 'corrupt')",
            ),
            _PENDING_INDEX,
        ]
    elif malformation == "unique_pending_index":
        statements = [current, _PENDING_INDEX.replace("CREATE INDEX", "CREATE UNIQUE INDEX")]
    elif malformation == "wrong_pending_index_order":
        statements = [current, (
            "CREATE INDEX turn_outbox_pending_idx "
            "ON turn_outbox(state, created_at, lease_expires_at, turn_id)"
        )]
    elif malformation == "unexpected_index":
        statements = [
            current, _PENDING_INDEX,
            "CREATE INDEX unexpected_idx ON turn_outbox(updated_at)",
        ]
    elif malformation == "unexpected_trigger":
        statements = [
            current, _PENDING_INDEX,
            "CREATE TRIGGER unexpected_trigger AFTER INSERT ON turn_outbox "
            "BEGIN UPDATE turn_outbox SET attempts=99 WHERE turn_id=NEW.turn_id; END",
        ]
    elif malformation == "unexpected_foreign_key":
        statements = [
            "CREATE TABLE parent_turn (turn_id TEXT PRIMARY KEY)",
            current.rstrip().removesuffix(")")
            + ", FOREIGN KEY(turn_id) REFERENCES parent_turn(turn_id))",
            _PENDING_INDEX,
        ]
    elif malformation == "wrong_application_id":
        statements = [current, _PENDING_INDEX]
        application_id += 1
    else:
        statements = [current, _PENDING_INDEX]
        user_version += 1
    _create_database(
        database, statements,
        application_id=application_id, user_version=user_version,
    )
    before = _logical_database_snapshot(database)

    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(database).prepare()

    assert _logical_database_snapshot(database) == before


def test_quick_check_failure_cannot_attest(tmp_path, monkeypatch):
    module = _load_client("colony_hermes_quick_check_attestation_test")
    database = tmp_path / "turn-outbox.sqlite3"
    module.TurnOutbox(database).prepare()
    original_connect = module.sqlite3.connect

    class _QuickCheckFailure:
        def fetchall(self):
            return [("database disk image is malformed",)]

        def fetchone(self):
            return ("database disk image is malformed",)

    class _Connection:
        def __init__(self, inner):
            object.__setattr__(self, "_inner", inner)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            if name == "_inner":
                object.__setattr__(self, name, value)
            else:
                setattr(self._inner, name, value)

        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.lower().split()) == "pragma quick_check":
                return _QuickCheckFailure()
            return self._inner.execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        module.sqlite3, "connect",
        lambda *args, **kwargs: _Connection(original_connect(*args, **kwargs)),
    )
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(database).prepare()


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_private_outbox_rejects_alias_without_mutating_target(tmp_path, alias_kind):
    module = _load_client(f"colony_hermes_private_outbox_{alias_kind}_test")
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    victim.chmod(0o644)
    database = tmp_path / "turn-outbox.sqlite3"
    if alias_kind == "symlink":
        database.symlink_to(victim)
    else:
        os.link(victim, database)
    before = (victim.read_bytes(), stat.S_IMODE(victim.stat().st_mode))

    with pytest.raises(module.PrivateSQLitePathError) as captured:
        module.TurnOutbox(database).prepare()

    assert (victim.read_bytes(), stat.S_IMODE(victim.stat().st_mode)) == before
    assert str(database) not in str(captured.value)
    assert str(victim) not in str(captured.value)


def test_private_outbox_rejects_symlink_parent_and_insecure_parent(tmp_path):
    module = _load_client("colony_hermes_private_outbox_parent_test")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(linked_parent / "turns.sqlite3").prepare()
    assert not (real_parent / "turns.sqlite3").exists()

    insecure_parent = tmp_path / "insecure-parent"
    insecure_parent.mkdir(mode=0o755)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(insecure_parent / "turns.sqlite3").prepare()
    assert stat.S_IMODE(insecure_parent.stat().st_mode) == 0o755


def test_private_outbox_rejects_relative_nonregular_and_wrong_owner_posture(
    tmp_path, monkeypatch,
):
    module = _load_client("colony_hermes_private_outbox_posture_test")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox("relative.sqlite3").prepare()

    nonregular = tmp_path / "nonregular"
    nonregular.mkdir(mode=0o700)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(nonregular).prepare()

    fifo = tmp_path / "nonregular-fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(fifo).prepare()

    database = tmp_path / "wrong-owner.sqlite3"
    database.touch(mode=0o600)
    real_euid = os.geteuid()
    monkeypatch.setattr(module.os, "geteuid", lambda: real_euid + 100_000)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(database).prepare()


def test_private_outbox_rejects_permissive_existing_file_without_chmod(tmp_path):
    module = _load_client("colony_hermes_private_outbox_mode_test")
    database = tmp_path / "turn-outbox.sqlite3"
    database.touch(mode=0o644)
    database.chmod(0o644)

    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(database).prepare()

    assert stat.S_IMODE(database.stat().st_mode) == 0o644


def test_private_outbox_detects_leaf_swap_across_sqlite_reopen(tmp_path, monkeypatch):
    module = _load_client("colony_hermes_private_outbox_swap_test")
    database = tmp_path / "turn-outbox.sqlite3"
    original_connect = module.sqlite3.connect
    swapped = False

    def swapping_connect(path, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.replace(path, tmp_path / "displaced.sqlite3")
            Path(path).touch(mode=0o600)
            Path(path).chmod(0o600)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", swapping_connect)
    with pytest.raises(module.PrivateSQLitePathError):
        module.TurnOutbox(database).prepare()
    assert swapped is True


def test_process_exit_leaves_fsynced_pending_turn_for_next_process(tmp_path):
    database = tmp_path / "turn-outbox.sqlite3"
    program = textwrap.dedent("""
        import importlib.util
        import json
        import sys

        spec = importlib.util.spec_from_file_location("outbox_child", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules["outbox_child"] = module
        spec.loader.exec_module(module)
        outbox = module.TurnOutbox(sys.argv[2])
        outbox.enqueue("turn-1", json.loads(sys.argv[3]))
    """)
    result = subprocess.run(
        [sys.executable, "-c", program, str(CLIENT_PATH), str(database),
         json.dumps(_payload())],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr

    module = _load_client()
    rows = module.TurnOutbox(database).snapshot()
    assert len(rows) == 1
    assert rows[0]["turn_id"] == "turn-1"
    assert rows[0]["state"] == "pending"
    assert rows[0]["payload"]["assistant_message"] == "delivered reply"
    assert os.stat(database).st_mode & 0o077 == 0


def test_restart_retries_pending_turn_and_keeps_durable_receipt(tmp_path):
    module = _load_client("colony_hermes_turn_outbox_retry_test")
    database = tmp_path / "turn-outbox.sqlite3"
    first_process = module.TurnOutbox(database)
    first_process.enqueue("turn-1", _payload())
    assert first_process.drain(
        lambda _payload, *, timeout_seconds: False,
    ) == 0
    assert first_process.snapshot()[0]["attempts"] == 1

    delivered = []
    restarted = module.TurnOutbox(database)
    assert restarted.drain(
        lambda value, *, timeout_seconds: delivered.append(value) or True,
    ) == 1
    assert delivered == [_payload()]
    row = restarted.snapshot()[0]
    assert row["state"] == "delivered"
    assert row["attempts"] == 2


def test_outbox_replay_is_idempotent_and_changed_envelope_conflicts(tmp_path):
    module = _load_client("colony_hermes_turn_outbox_idempotency_test")
    outbox = module.TurnOutbox(tmp_path / "turn-outbox.sqlite3")
    first = outbox.enqueue("turn-1", _payload())
    replay = outbox.enqueue("turn-1", _payload())
    assert first["envelope_sha256"] == replay["envelope_sha256"]
    with pytest.raises(module.TurnOutboxConflict):
        outbox.enqueue("turn-1", _payload("changed reply"))

    deliveries = []
    assert outbox.drain(
        lambda value, *, timeout_seconds: deliveries.append(value) or True,
    ) == 1
    assert outbox.enqueue("turn-1", _payload())["state"] == "delivered"
    assert outbox.drain(
        lambda value, *, timeout_seconds: deliveries.append(value) or True,
    ) == 0
    assert deliveries == [_payload()]


def test_outbox_rejects_unbounded_or_noncanonical_payload(tmp_path):
    module = _load_client("colony_hermes_turn_outbox_bounds_test")
    outbox = module.TurnOutbox(
        tmp_path / "turn-outbox.sqlite3", max_payload_bytes=4096,
    )
    with pytest.raises(module.TurnOutboxPayloadError):
        outbox.enqueue("turn-large", {"text": "x" * 5000})
    with pytest.raises(module.TurnOutboxPayloadError):
        outbox.enqueue("turn-nan", {"score": float("nan")})
    assert outbox.snapshot() == []


def test_hung_delivery_does_not_lock_enqueue_and_expired_lease_recovers(tmp_path):
    module = _load_client("colony_hermes_turn_outbox_lease_test")
    database = tmp_path / "turn-outbox.sqlite3"
    outbox = module.TurnOutbox(database)
    outbox.enqueue("turn-1", _payload())
    started = threading.Event()
    release = threading.Event()
    server: dict[str, str] = {}
    server_lock = threading.Lock()

    def exact_idempotent_put(value, *, timeout_seconds):
        started.set()
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with server_lock:
            previous = server.setdefault(value["turn_id"], digest)
            assert previous == digest
        if not release.wait(timeout_seconds):
            raise TimeoutError("remote outcome unknown at cooperative deadline")
        return True

    first = threading.Thread(
        target=lambda: outbox.drain(
            exact_idempotent_put, limit=1, timeout_seconds=0.05,
            lease_seconds=0.10,
        ),
    )
    first.start()
    assert started.wait(1)
    began = time.monotonic()
    outbox.enqueue("turn-2", {**_payload(), "turn_id": "turn-2"})
    assert time.monotonic() - began < 0.25
    first.join(1)
    assert not first.is_alive()
    row = {item["turn_id"]: item for item in outbox.snapshot()}["turn-1"]
    assert row["state"] == "pending"
    # A slow disk may consume the finalization reserve. The pending lease is
    # the durable recovery contract even when its diagnostic cannot be saved.
    assert row["last_error"] in {"", "delivery_outcome_unknown"}
    assert row["lease_id"]

    # The remote may have accepted before its timeout was observed. There is no
    # local callback continuing after return; the durable retry still uses the
    # same exact PUT identity/content and cannot create a conflict.
    assert "turn-1" in server
    release.set()
    deadline = time.time() + 1
    while time.time() < deadline and row["lease_expires_at"] > time.time():
        time.sleep(0.005)
    restarted = module.TurnOutbox(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: restarted.drain(
                exact_idempotent_put, limit=1, timeout_seconds=0.2,
                lease_seconds=0.2,
            ),
            range(2),
        ))
    # turn-2 may be claimed before the expired turn-1, but every server write
    # remains keyed to one canonical turn and no content can diverge.
    while restarted.snapshot()[0]["state"] != "delivered":
        assert restarted.drain(
            exact_idempotent_put, limit=1, timeout_seconds=0.2,
        ) in {0, 1}
    final = {item["turn_id"]: item for item in restarted.snapshot()}
    assert final["turn-1"]["state"] == "delivered"
    assert server["turn-1"] == final["turn-1"]["envelope_sha256"]
    assert sum(results) in {1, 2}


def test_delivery_exception_is_redacted_and_retryable(tmp_path):
    module = _load_client("colony_hermes_turn_outbox_redaction_test")
    outbox = module.TurnOutbox(tmp_path / "turn-outbox.sqlite3")
    outbox.enqueue("turn-1", _payload())

    def secret_failure(_value, *, timeout_seconds):
        raise RuntimeError("bearer super-secret-value")

    assert outbox.drain(secret_failure, limit=1) == 0
    row = outbox.snapshot()[0]
    assert row["last_error"] == "delivery_exception"
    assert "secret" not in row["last_error"]
    assert row["lease_id"] == ""
    assert outbox.drain(
        lambda _value, *, timeout_seconds: True, limit=1,
    ) == 1


class _Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return dict(self.value)

    def raise_for_status(self):
        return None


class _Client:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.synced = []
        self.guard_verdict = {}
        self.sync_block = None
        self.sync_budgets = []
        self.__class__.instances.append(self)

    def get(self, path, **_kwargs):
        assert path == "/v1/host/contacts/resolve"
        return _Response({"contact_id": "cid-owner"})

    def post(self, path, **_kwargs):
        assert path == "/v1/host/response-guard/check"
        return _Response(self.guard_verdict)

    def sync_turn(self, **kwargs):
        self.sync_budgets.append(float(kwargs["timeout_seconds"]))
        if self.sync_block is not None:
            if not self.sync_block.wait(float(kwargs["timeout_seconds"])):
                raise TimeoutError("cooperative test timeout")
        self.synced.append(kwargs)
        return True


class _Context:
    def __init__(self, outbox_path, *, drain_timeout_ms=250):
        self.config = {"plugins": {"colony": {
            "url": "http://colony.test",
            "owner_contact_id": "cid-owner",
            "turn_outbox_path": str(outbox_path),
            "turn_outbox_drain_timeout_ms": drain_timeout_ms,
        }}}
        self.tools = {}
        self.hooks = {}
        self.middleware = {}
        self.commands = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, name, fn):
        self.middleware[name] = fn

    def register_command(self, name, fn, **_kwargs):
        self.commands[name] = fn


def test_registration_validates_private_outbox_before_exposing_hooks(
    tmp_path, monkeypatch,
):
    module = _load_plugin("colony_hermes_private_outbox_registration_test")
    module.ColonyClient = _Client
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    victim.chmod(0o644)
    database = tmp_path / "turn-outbox.sqlite3"
    database.symlink_to(victim)
    context = _Context(database)

    with pytest.raises(module.PrivateSQLitePathError):
        module.register(context)

    assert context.tools == {}
    assert context.hooks == {}
    assert context.middleware == {}
    assert context.commands == {}
    assert victim.read_bytes() == b""
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_registration_rejects_malformed_schema_before_exposing_surfaces(
    tmp_path, monkeypatch,
):
    module = _load_plugin("colony_hermes_malformed_registration_test")
    module.ColonyClient = _Client
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    database = tmp_path / "turn-outbox.sqlite3"
    _create_database(
        database,
        ["CREATE TABLE turn_outbox (turn_id TEXT PRIMARY KEY)"],
    )
    before = _logical_database_snapshot(database)
    context = _Context(database)

    with pytest.raises(module.PrivateSQLitePathError):
        module.register(context)

    assert context.tools == {}
    assert context.hooks == {}
    assert context.middleware == {}
    assert context.commands == {}
    assert _logical_database_snapshot(database) == before


def test_writer_records_guard_replacement_that_pinned_hermes_delivers(
    tmp_path, monkeypatch,
):
    module = _load_plugin()
    _Client.instances.clear()
    module.ColonyClient = _Client
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    database = tmp_path / "turn-outbox.sqlite3"
    context = _Context(database)
    module.register(context)
    client = _Client.instances[-1]
    context.hooks["pre_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="turn-1",
        platform="sms", sender_id="+15550001", user_message="hello",
    )
    candidate = "blocked original"
    client.guard_verdict = {
        "decision": "block",
        "mode": "enforce",
        "surface": "text_chat",
        "surface_family": "text",
        "applicability": "guarded",
        "guard_status": "evaluated",
        "policy_id": module._GUARD_POLICY_ID,
        "policy_digest": module._GUARD_POLICY_DIGEST,
        "candidate_digest": hashlib.sha256(candidate.encode()).hexdigest(),
        "findings": [],
    }

    # Exact v2026.7.7.2 order: transform first, then post receives the replaced
    # final_response. This simulates those two pinned calls in order.
    replacement = context.hooks["transform_llm_output"](
        response_text=candidate, session_id="session-1", model="model-a",
        platform="sms",
    )
    assert replacement == module._GUARD_WITHHELD_TEXT
    context.hooks["post_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="turn-1",
        user_message="hello", assistant_response=replacement,
        conversation_history=[], model="model-a", platform="sms",
    )

    assert len(client.synced) == 1
    assert client.synced[0]["assistant_message"] == module._GUARD_WITHHELD_TEXT
    rows = module.TurnOutbox(database).snapshot()
    assert rows[0]["state"] == "delivered"
    assert rows[0]["payload"]["assistant_message"] == module._GUARD_WITHHELD_TEXT
    assert module.SUPPORTED_HERMES_TURN_FINALIZER == {
        "tag": "v2026.7.7.2",
        "commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
        "sha256": "01602214acdb686338fa93580e3fe6ae1bdbc4731f246df0ba1f749ca2930663",
        "transform_precedes_post": True,
        "post_receives_transformed_response": True,
    }


def test_colony_outage_never_withholds_safe_reply_after_durable_enqueue(
    tmp_path, monkeypatch,
):
    module = _load_plugin("colony_hermes_turn_outbox_outage_plugin_test")
    _Client.instances.clear()
    module.ColonyClient = _Client
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    database = tmp_path / "turn-outbox.sqlite3"
    context = _Context(database, drain_timeout_ms=40)
    module.register(context)
    client = _Client.instances[-1]
    client.sync_block = threading.Event()
    context.hooks["pre_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="turn-outage",
        platform="sms", sender_id="+15550001", user_message="hello",
    )
    began = time.monotonic()
    result = context.hooks["post_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="turn-outage",
        user_message="hello", assistant_response="safe reply",
        conversation_history=[], model="model-a", platform="sms",
    )
    elapsed = time.monotonic() - began
    assert result is None
    # Includes the mandatory FULL-synchronous enqueue on the runner's disk.
    # The delivery callback itself still receives the configured 40 ms bound.
    assert elapsed < 1.0
    assert all(0 < budget <= 0.04 for budget in client.sync_budgets)
    row = module.TurnOutbox(database).snapshot()[0]
    assert row["state"] == "pending"
    assert row["payload"]["assistant_message"] == "safe reply"
    assert row["last_error"] in {"", "delivery_outcome_unknown", "delivery_budget_exhausted"}
    client.sync_block.set()


def test_post_turn_drain_shrinks_recovered_backlog_within_one_total_budget(
    tmp_path, monkeypatch,
):
    module = _load_plugin("colony_hermes_post_turn_backlog_drain_test")
    _Client.instances.clear()
    module.ColonyClient = _Client
    monkeypatch.setenv("COLONY_GENERAL_PLUGIN_ACTIVE", "1")
    monkeypatch.setenv("COLONY_MEMORY_WORKER_TOOLS", "0")
    monkeypatch.setenv("COLONY_MEMORY_TURN_WRITER", "disabled")
    database = tmp_path / "turn-outbox.sqlite3"
    context = _Context(database, drain_timeout_ms=250)
    module.register(context)
    outbox = module.TurnOutbox(database)
    for index in range(3):
        turn_id = f"recovered-{index}"
        outbox.enqueue(turn_id, {**_payload(), "turn_id": turn_id})
    client = _Client.instances[-1]
    context.hooks["pre_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="turn-current",
        platform="sms", sender_id="+15550001", user_message="hello",
    )

    began = time.monotonic()
    context.hooks["post_llm_call"](
        session_id="session-1", task_id="task-1", turn_id="turn-current",
        user_message="hello", assistant_response="safe reply",
        conversation_history=[], model="model-a", platform="sms",
    )

    assert time.monotonic() - began < 0.5
    assert len(client.synced) == 4
    assert all(row["state"] == "delivered" for row in outbox.snapshot())


def test_explicit_recovery_drain_is_caller_driven_and_bounded(tmp_path):
    module = _load_plugin("colony_hermes_explicit_recovery_drain_test")
    database = tmp_path / "turn-outbox.sqlite3"
    outbox = module.TurnOutbox(database)
    for index in range(5):
        turn_id = f"recovered-{index}"
        outbox.enqueue(turn_id, {**_payload(), "turn_id": turn_id})
    delivered = []

    count = module.recover_turn_outbox(
        {"turn_outbox_path": str(database)},
        lambda payload, *, timeout_seconds: (
            delivered.append(payload["turn_id"]) or True
        ),
        limit=2,
        timeout_seconds=1.0,
    )

    assert count == 2
    assert delivered == ["recovered-0", "recovered-1"]
    assert sum(row["state"] == "pending" for row in outbox.snapshot()) == 3


def test_bounded_drain_does_not_repeat_full_schema_check_per_row(
    tmp_path, monkeypatch,
):
    module = _load_client("colony_hermes_drain_schema_check_budget_test")
    database = tmp_path / "turn-outbox.sqlite3"
    writer = module.TurnOutbox(database)
    writer.prepare()
    for index in range(8):
        turn_id = f"recovered-{index}"
        writer.enqueue(turn_id, {**_payload(), "turn_id": turn_id})

    full_checks = 0
    original = module.TurnOutbox._quick_check

    def counted(connection):
        nonlocal full_checks
        full_checks += 1
        return original(connection)

    monkeypatch.setattr(
        module.TurnOutbox, "_quick_check", staticmethod(counted),
    )
    # This is a schema-cache test, not a filesystem throughput benchmark.
    # Freeze the cooperative clock so slow fullfsync implementations cannot
    # consume the wall budget before all rows exercise the cached path. The
    # neighboring lock and callback tests retain the real monotonic clock and
    # continue to enforce the product's total wall-budget semantics.
    monkeypatch.setattr(module.time, "monotonic", lambda: 1_000.0)
    restarted = module.TurnOutbox(database)

    assert restarted.drain(
        lambda _payload, *, timeout_seconds: True,
        limit=8,
        timeout_seconds=0.25,
    ) == 8
    # One first-use validation may check before and after a recognized schema;
    # claims/finalizations for each row must use only cached cheap invariants.
    assert full_checks <= 2


def test_drain_database_lock_wait_is_inside_total_wall_budget(tmp_path):
    module = _load_client("colony_hermes_drain_lock_budget_test")
    database = tmp_path / "turn-outbox.sqlite3"
    outbox = module.TurnOutbox(database)
    outbox.enqueue("turn-1", _payload())
    locker = sqlite3.connect(database, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    began = time.monotonic()
    try:
        result = outbox.drain(
            lambda _payload, *, timeout_seconds=None: True,
            limit=1,
            timeout_seconds=0.05,
        )
    finally:
        elapsed = time.monotonic() - began
        locker.rollback()
        locker.close()

    assert result == 0
    assert elapsed < 0.20
    row = outbox.snapshot()[0]
    assert row["state"] == "pending"
    assert row["attempts"] == 0


def test_drain_schema_lock_wait_is_inside_total_wall_budget(tmp_path):
    module = _load_client("colony_hermes_drain_schema_lock_budget_test")
    database = tmp_path / "turn-outbox.sqlite3"
    outbox = module.TurnOutbox(database)
    outbox.enqueue("turn-1", _payload())
    locked = threading.Event()
    release = threading.Event()

    def hold_schema_lock():
        with outbox._schema_lock:
            locked.set()
            release.wait(1.0)

    holder = threading.Thread(target=hold_schema_lock)
    holder.start()
    assert locked.wait(1.0)
    delayed_release = threading.Timer(0.30, release.set)
    delayed_release.start()
    began = time.monotonic()
    try:
        result = outbox.drain(
            lambda _payload, *, timeout_seconds=None: True,
            limit=1,
            timeout_seconds=0.05,
        )
    finally:
        elapsed = time.monotonic() - began
        release.set()
        holder.join(1.0)
        delayed_release.cancel()

    assert not holder.is_alive()
    assert result == 0
    # Leave generous scheduler headroom while proving that the internal Python
    # lock cannot consume the deliberately delayed 300 ms release.
    assert elapsed < 0.20
    row = outbox.snapshot()[0]
    assert row["state"] == "pending"
    assert row["attempts"] == 0


def test_drain_finalize_lock_wait_is_inside_same_total_budget(tmp_path):
    module = _load_client("colony_hermes_finalize_lock_budget_test")
    database = tmp_path / "turn-outbox.sqlite3"
    outbox = module.TurnOutbox(database)
    outbox.enqueue("turn-1", _payload())
    locker = sqlite3.connect(database, isolation_level=None)

    def accepted_then_lock(_payload, *, timeout_seconds=None):
        locker.execute("BEGIN IMMEDIATE")
        return True

    began = time.monotonic()
    try:
        result = outbox.drain(
            accepted_then_lock, limit=1, timeout_seconds=0.05,
        )
    finally:
        elapsed = time.monotonic() - began
        locker.rollback()
        locker.close()

    assert result == 0
    assert elapsed < 0.20
    row = outbox.snapshot()[0]
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["lease_id"]


def test_drain_has_no_unbudgeted_explicit_fsync_tail(tmp_path, monkeypatch):
    module = _load_client("colony_hermes_drain_fsync_budget_test")
    outbox = module.TurnOutbox(tmp_path / "turn-outbox.sqlite3")
    outbox.enqueue("turn-1", _payload())
    explicit_syncs = []
    monkeypatch.setattr(outbox, "_fsync_storage", lambda: explicit_syncs.append(True))

    result = outbox.drain(
        lambda _payload, *, timeout_seconds=None: True,
        limit=1,
        timeout_seconds=1.0,
    )

    assert result == 1
    assert explicit_syncs == []


def test_delivery_callback_is_cooperative_same_thread_and_receives_budget(
    tmp_path,
):
    module = _load_client("colony_hermes_cooperative_delivery_test")
    outbox = module.TurnOutbox(tmp_path / "turn-outbox.sqlite3")
    outbox.enqueue("turn-1", _payload())
    caller_thread = threading.get_ident()
    observed = []

    def deliver(_payload, *, timeout_seconds=None):
        observed.append((threading.get_ident(), timeout_seconds))
        return True

    assert outbox.drain(deliver, limit=1, timeout_seconds=0.20) == 1
    assert len(observed) == 1
    assert observed[0][0] == caller_thread
    assert isinstance(observed[0][1], float)
    assert 0 < observed[0][1] < 0.20


def test_colony_client_timeout_is_fixed_ambiguous_outcome(monkeypatch):
    module = _load_client("colony_hermes_client_timeout_truth_test")

    class _TimeoutClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def put(self, *_args, **_kwargs):
            raise module.httpx.ReadTimeout("secret remote timeout detail")

    monkeypatch.setattr(module.httpx, "Client", _TimeoutClient)
    client = module.ColonyClient("http://127.0.0.1:7777")

    with pytest.raises(
        module.TurnDeliveryOutcomeUnknown,
        match="participant-bound turn outcome is unknown",
    ) as captured:
        client.sync_turn(
            session_id="session-1",
            contact_id="cid-owner",
            turn_id="turn-1",
            timeout_seconds=0.05,
        )
    assert "secret" not in str(captured.value)


def test_colony_client_uses_one_absolute_deadline_across_http_phases():
    module = _load_client("colony_hermes_client_absolute_deadline_test")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1.0)
    port = int(listener.getsockname()[1])
    request_seen = threading.Event()
    unexpected_errors = []

    def serve_slow_phases():
        try:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(1.0)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += connection.recv(65_536)
                headers, body = request.split(b"\r\n\r\n", 1)
                content_length = 0
                for line in headers.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.lower() == b"content-length":
                        content_length = int(value.strip())
                while len(body) < content_length:
                    body += connection.recv(65_536)
                request_seen.set()

                response_body = b'{"accepted":true}'
                response_headers = (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 17\r\nConnection: close\r\n\r\n"
                )
                # Every individual wait is below the old 50 ms scalar timeout,
                # while the complete response deliberately exceeds 50 ms.
                for chunk in (
                    response_headers,
                    response_body[:8],
                    response_body[8:],
                ):
                    time.sleep(0.035)
                    connection.sendall(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The fixed client closes the socket at its absolute deadline.
            pass
        except BaseException as error:  # pragma: no cover - diagnostic guard
            unexpected_errors.append(type(error).__name__)
        finally:
            listener.close()

    server = threading.Thread(target=serve_slow_phases, daemon=True)
    server.start()
    client = module.ColonyClient(f"http://127.0.0.1:{port}")
    began = time.monotonic()
    try:
        with pytest.raises(
            module.TurnDeliveryOutcomeUnknown,
            match="participant-bound turn outcome is unknown",
        ):
            client.sync_turn(
                session_id="session-1",
                contact_id="cid-owner",
                turn_id="turn-absolute-deadline",
                timeout_seconds=0.05,
            )
        elapsed = time.monotonic() - began
    finally:
        server.join(1.0)
        if server.is_alive():
            listener.close()
            server.join(1.0)

    assert not server.is_alive()
    assert request_seen.is_set()
    assert unexpected_errors == []
    assert elapsed < 0.20


def test_colony_client_absolute_deadline_transport_accepts_exact_put():
    module = _load_client("colony_hermes_client_deadline_success_test")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1.0)
    port = int(listener.getsockname()[1])
    captured = []

    def serve_once():
        try:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(1.0)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += connection.recv(65_536)
                headers, body = request.split(b"\r\n\r\n", 1)
                content_length = next(
                    int(line.partition(b":")[2].strip())
                    for line in headers.split(b"\r\n")[1:]
                    if line.partition(b":")[0].lower() == b"content-length"
                )
                while len(body) < content_length:
                    body += connection.recv(65_536)
                captured.append((headers, body[:content_length]))
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 17\r\nConnection: close\r\n\r\n"
                    b'{"accepted":true}'
                )
        finally:
            listener.close()

    server = threading.Thread(target=serve_once, daemon=True)
    server.start()
    try:
        client = module.ColonyClient(
            f"http://127.0.0.1:{port}", api_key="scoped-test-key",
        )
        assert client.sync_turn(
            session_id="session-1",
            contact_id="cid-owner",
            turn_id="turn/one",
            timeout_seconds=0.20,
        ) is True
    finally:
        server.join(1.0)
        if server.is_alive():
            listener.close()
            server.join(1.0)

    assert not server.is_alive()
    assert len(captured) == 1
    headers, body = captured[0]
    assert headers.startswith(b"PUT /v2/host/turns/turn%2Fone HTTP/1.1\r\n")
    assert b"authorization: bearer scoped-test-key\r\n" in headers.lower()
    assert json.loads(body) == {
        "context": {
            "contact_id": "cid-owner",
            "session_id": "session-1",
            "turn_id": "turn/one",
        },
        "identity": {"host_id": "hermes"},
    }


def test_colony_client_deadline_path_never_uses_unbounded_dns(monkeypatch):
    module = _load_client("colony_hermes_client_no_dns_test")
    dns_calls = 0

    def counted_getaddrinfo(*_args, **_kwargs):
        nonlocal dns_calls
        dns_calls += 1
        raise OSError("audit DNS probe")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        counted_getaddrinfo,
    )
    client = module.ColonyClient("http://colony.internal:7777")

    began = time.monotonic()
    assert client.sync_turn(
        session_id="session-1",
        contact_id="cid-owner",
        turn_id="turn-no-dns",
        timeout_seconds=0.05,
    ) is False

    assert dns_calls == 0
    assert time.monotonic() - began < 0.20


def test_durability_attestation_distinguishes_configuration_from_physical_proof(
    tmp_path,
):
    module = _load_client("colony_hermes_durability_truth_test")
    value = module.TurnOutbox(tmp_path / "turn-outbox.sqlite3").prepare()

    assert value["schema"] == "PrivateSQLiteDurabilityConfigurationAttestationV2"
    assert value["version"] == 2
    assert value["configuration_ready"] is True
    assert value["physical_power_loss_verified"] is False
    assert value["readiness_scope"] == "sqlite_and_filesystem_configuration"
    assert "ready" not in value

    readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    spec = (PLUGIN_DIR / "SPEC.md").read_text(encoding="utf-8")
    for document in (readme, spec):
        assert "physical_power_loss_verified=false" in document
        assert "configuration readiness" in document.lower()
        assert "FULL-durability outbox" not in document
