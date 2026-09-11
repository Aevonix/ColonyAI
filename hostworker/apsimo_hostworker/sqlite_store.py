"""Reference :class:`~colony_hostworker.store.ActionStore` on local SQLite.

This is the governed-action SUBSET only: actions, immutable receipts and
events, dead letters, leases, the owner-authorized dispatch transaction, and
the bounded GET-only observation machinery.  It deliberately omits the
general-purpose queue features of the host system it was extracted from
(callback outboxes, message-delivery lifecycles, policy-grant dispatch);
adding them back here would widen the surface the conformance suite must
defend.

Durability posture: WAL journal, ``synchronous=FULL``, ``BEGIN IMMEDIATE``
transactions, owner-only 0600 database files, and SQL triggers that make
receipts, events, and action identity immutable even against buggy code in
this very module.

Every transactional invariant this store upholds is specified in
:mod:`colony_hostworker.store` (I1-I11) and exercised by
:mod:`colony_hostworker.conformance`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Mapping, Sequence

from .catalog import GRANT_AUTHORIZABLE_TOOL_NAMES
from .contract import canonical_json_utf8, sha256_json_utf8
from .gate import GRANT_BINDING_METHOD, GateAuthorization
from .store import (
    ALLOWED_TRANSITIONS,
    ActionIdempotencyConflict,
    ActionLeaseConflict,
    ActionNotFound,
    ActionStoreError,
    ActionTransitionError,
    GATE_RECEIPT_KIND,
    OBSERVATION_RECEIPT_KIND,
    RECOVERY_RECEIPT_KIND,
    STATE_ACCEPTED,
    STATE_COMPLETED,
    STATE_DISPATCHED,
    STATE_FAILED,
    STATE_GATED,
    STATE_PROPOSED,
    STATE_VERIFIED,
    TERMINAL_STATES,
)

RECOVERY_SCHEMA = "ColonyDispatchObservationRecoveryV1"
OBSERVATION_SCHEMA = "ColonyDispatchObservationAttemptV1"

_RESERVED_RECEIPT_KINDS = frozenset(
    {GATE_RECEIPT_KIND, RECOVERY_RECEIPT_KIND, OBSERVATION_RECEIPT_KIND}
)

_ALL_STATES = (
    STATE_PROPOSED,
    STATE_GATED,
    STATE_DISPATCHED,
    STATE_ACCEPTED,
    STATE_VERIFIED,
    STATE_COMPLETED,
    STATE_FAILED,
)

_LEASEABLE_STATES = frozenset({STATE_GATED, STATE_ACCEPTED, STATE_VERIFIED})

_GATE_CLOCK_SKEW_SECONDS = 30.0


class SqliteActionStore:
    """SQLite-backed governed-action lifecycle store."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str, *, clock=None, busy_timeout_ms: int = 5000):
        import time as _time

        self.path = os.path.abspath(os.path.expanduser(path))
        self._clock = clock or _time.time
        self._lock = threading.RLock()

        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        # SQLite creates the database, WAL, and shared-memory files lazily.
        # A restrictive creation mask prevents payloads and receipts from
        # being briefly world-readable before the explicit chmod below.
        previous_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(
                self.path,
                timeout=max(float(busy_timeout_ms) / 1000.0, 0.001),
                isolation_level=None,
                check_same_thread=False,
            )
        finally:
            os.umask(previous_umask)
        self._conn.row_factory = sqlite3.Row
        existing_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if existing_version not in (0, self.SCHEMA_VERSION):
            self.close()
            raise ActionStoreError(
                "unsupported action-store schema version %s" % existing_version
            )
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
        try:
            self._create_schema()
            self._secure_database_files()
        except Exception:
            self.close()
            raise

    # ------------------------------------------------------------- plumbing

    def _secure_database_files(self):
        private_mode = stat.S_IRUSR | stat.S_IWUSR
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                os.chmod(candidate, private_mode)
            except FileNotFoundError:
                continue

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._conn is None:
                raise ActionStoreError("action store is closed")
            cursor = self._conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                yield cursor
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
                self._secure_database_files()
            finally:
                cursor.close()

    def _create_schema(self):
        states = ",".join("'%s'" % state for state in _ALL_STATES)
        ddl = """
        CREATE TABLE IF NOT EXISTS actions (
            action_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            source_ref TEXT,
            action_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (%s)),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            max_attempts INTEGER NOT NULL CHECK (max_attempts = 1),
            next_attempt_at REAL NOT NULL,
            lease_owner TEXT,
            lease_expires_at REAL,
            last_error TEXT,
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            terminal_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_actions_ready
            ON actions(state, next_attempt_at, lease_expires_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_actions_source_ref
            ON actions(source, source_ref);

        CREATE TABLE IF NOT EXISTS action_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT NOT NULL REFERENCES actions(action_id),
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_action_events_action
            ON action_events(action_id, event_id);

        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL REFERENCES actions(action_id),
            receipt_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            external_id TEXT,
            evidence_json TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            observed_at REAL NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(action_id, receipt_key)
        );
        CREATE INDEX IF NOT EXISTS idx_receipts_action
            ON receipts(action_id, created_at);

        CREATE TABLE IF NOT EXISTS dead_letters (
            dead_letter_id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
            reason TEXT NOT NULL,
            action_snapshot_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS action_events_no_update
        BEFORE UPDATE ON action_events BEGIN
            SELECT RAISE(ABORT, 'action events are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS action_events_no_delete
        BEFORE DELETE ON action_events BEGIN
            SELECT RAISE(ABORT, 'action events are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS receipts_no_update
        BEFORE UPDATE ON receipts BEGIN
            SELECT RAISE(ABORT, 'receipts are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS receipts_no_delete
        BEFORE DELETE ON receipts BEGIN
            SELECT RAISE(ABORT, 'receipts are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS actions_identity_no_update
        BEFORE UPDATE OF action_id,idempotency_key,source,source_ref,action_type,
                         payload_json,payload_sha256,max_attempts,created_at
        ON actions BEGIN
            SELECT RAISE(ABORT, 'action identity and payload are immutable');
        END;
        """ % states
        with self._lock:
            try:
                self._conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + ddl
                    + "\nPRAGMA user_version=%d;\nCOMMIT;" % self.SCHEMA_VERSION
                )
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _validate_text(name, value):
        if not isinstance(value, str) or not value.strip():
            raise ActionStoreError("%s must be a non-empty string" % name)
        return value.strip()

    @staticmethod
    def _json_load(value):
        return json.loads(value) if value is not None else None

    def _action_dict(self, row):
        if row is None:
            return None
        result = dict(row)
        raw_payload = result.pop("payload_json")
        try:
            payload = self._json_load(raw_payload)
            canonical = canonical_json_utf8(payload)
        except Exception as exc:
            raise ActionStoreError("action payload is not canonical JSON") from exc
        observed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(observed, str(result.get("payload_sha256") or "")):
            raise ActionStoreError(
                "action payload digest does not match its immutable record"
            )
        result["payload"] = payload
        result["result"] = self._json_load(result.pop("result_json"))
        return result

    def _receipt_dict(self, row):
        result = dict(row)
        result["evidence"] = self._json_load(result.pop("evidence_json"))
        return result

    @staticmethod
    def _get_action_row(cursor, action_id):
        row = cursor.execute(
            "SELECT * FROM actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise ActionNotFound("action %s not found" % action_id)
        return row

    @staticmethod
    def _require_lease(row, owner, now):
        if not owner or row["lease_owner"] != owner:
            raise ActionLeaseConflict(
                "record is not leased by %s" % (owner or "<empty>")
            )
        expires = row["lease_expires_at"]
        if expires is None or float(expires) <= now:
            raise ActionLeaseConflict("lease for %s has expired" % owner)

    @staticmethod
    def _check_transition(from_state, to_state):
        if to_state not in ALLOWED_TRANSITIONS.get(from_state, frozenset()):
            raise ActionTransitionError(
                "cannot transition %s -> %s" % (from_state, to_state)
            )

    @staticmethod
    def _add_event(cursor, action_id, event_type, from_state, to_state, actor, details, now):
        cursor.execute(
            """INSERT INTO action_events
               (action_id,event_type,from_state,to_state,actor,details_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                action_id,
                event_type,
                from_state,
                to_state,
                actor,
                canonical_json_utf8(details or {}),
                now,
            ),
        )

    def _transition(
        self,
        cursor,
        row,
        to_state,
        actor,
        event_type,
        details,
        now,
        result=None,
        error=None,
        clear_lease=False,
        next_attempt_at=None,
    ):
        from_state = row["state"]
        self._check_transition(from_state, to_state)
        terminal_at = now if to_state in TERMINAL_STATES else None
        result_json = (
            canonical_json_utf8(result) if result is not None else row["result_json"]
        )
        lease_owner = None if clear_lease else row["lease_owner"]
        lease_expires = None if clear_lease else row["lease_expires_at"]
        next_at = row["next_attempt_at"] if next_attempt_at is None else next_attempt_at
        cursor.execute(
            """UPDATE actions
               SET state=?, result_json=?, last_error=?, next_attempt_at=?,
                   lease_owner=?, lease_expires_at=?, updated_at=?, terminal_at=?
               WHERE action_id=?""",
            (
                to_state,
                result_json,
                error,
                next_at,
                lease_owner,
                lease_expires,
                now,
                terminal_at,
                row["action_id"],
            ),
        )
        self._add_event(
            cursor,
            row["action_id"],
            event_type,
            from_state,
            to_state,
            actor,
            details,
            now,
        )

    def _dead_letter_action(self, cursor, action_id, reason, now):
        row = self._get_action_row(cursor, action_id)
        snapshot = self._action_dict(row)
        cursor.execute(
            """INSERT OR IGNORE INTO dead_letters
               (dead_letter_id,action_id,reason,action_snapshot_json,created_at)
               VALUES (?,?,?,?,?)""",
            (str(uuid.uuid4()), action_id, reason, canonical_json_utf8(snapshot), now),
        )

    # ------------------------------------------------------------- ingress

    def propose(
        self,
        idempotency_key,
        source,
        action_type,
        payload,
        source_ref=None,
        action_id=None,
        actor="ingress",
    ):
        """Insert one immutable governed action in ``proposed`` state.

        ``max_attempts`` is pinned to 1 for every governed action — a schema
        CHECK re-enforces it — so the one-mutation guarantee cannot be
        configured away at ingress.
        """

        idempotency_key = self._validate_text("idempotency_key", idempotency_key)
        source = self._validate_text("source", source)
        action_type = self._validate_text("action_type", action_type)
        if source_ref is not None and not isinstance(source_ref, str):
            raise ActionStoreError("source_ref must be a string or None")
        payload_json = canonical_json_utf8(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = float(self._clock())
        action_id = action_id or str(uuid.uuid4())

        with self._transaction() as cursor:
            existing = cursor.execute(
                "SELECT * FROM actions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["source"] == source
                    and existing["source_ref"] == source_ref
                    and existing["action_type"] == action_type
                    and existing["payload_sha256"] == payload_hash
                )
                if not same:
                    raise ActionIdempotencyConflict(
                        "action idempotency key %s was reused with different content"
                        % idempotency_key
                    )
                return self._action_dict(existing)

            cursor.execute(
                """INSERT INTO actions
                   (action_id,idempotency_key,source,source_ref,action_type,payload_json,
                    payload_sha256,state,attempt_count,max_attempts,next_attempt_at,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id,
                    idempotency_key,
                    source,
                    source_ref,
                    action_type,
                    payload_json,
                    payload_hash,
                    STATE_PROPOSED,
                    0,
                    1,
                    now,
                    now,
                    now,
                ),
            )
            self._add_event(
                cursor,
                action_id,
                "proposed",
                None,
                STATE_PROPOSED,
                actor,
                {
                    "source": source,
                    "source_ref": source_ref,
                    "action_type": action_type,
                },
                now,
            )
            return self._action_dict(self._get_action_row(cursor, action_id))

    def _insert_receipt(
        self,
        cursor,
        action_id,
        receipt_key,
        kind,
        status_value,
        evidence,
        external_id,
        observed_at,
        now,
    ):
        receipt_key = self._validate_text("receipt_key", receipt_key)
        kind = self._validate_text("kind", kind)
        status_value = self._validate_text("status", status_value)
        evidence_json = canonical_json_utf8(evidence)
        evidence_hash = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        existing = cursor.execute(
            "SELECT * FROM receipts WHERE action_id=? AND receipt_key=?",
            (action_id, receipt_key),
        ).fetchone()
        if existing is not None:
            same = (
                existing["kind"] == kind
                and existing["status"] == status_value
                and existing["external_id"] == external_id
                and existing["evidence_sha256"] == evidence_hash
            )
            if not same:
                raise ActionIdempotencyConflict(
                    "receipt key %s was reused with different evidence" % receipt_key
                )
            return self._receipt_dict(existing)
        receipt_id = str(uuid.uuid4())
        cursor.execute(
            """INSERT INTO receipts
               (receipt_id,action_id,receipt_key,kind,status,external_id,evidence_json,
                evidence_sha256,observed_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                receipt_id,
                action_id,
                receipt_key,
                kind,
                status_value,
                external_id,
                evidence_json,
                evidence_hash,
                float(observed_at if observed_at is not None else now),
                now,
            ),
        )
        return self._receipt_dict(
            cursor.execute(
                "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
        )

    def add_receipt(
        self,
        action_id,
        receipt_key,
        kind,
        status_value,
        evidence,
        external_id=None,
        observed_at=None,
    ):
        """Append one generic receipt.  Reserved kinds are refused here: the
        gate receipt is written by :meth:`gate` and the recovery/observation
        receipts only by their transactional owners."""

        kind = self._validate_text("kind", kind)
        if kind in _RESERVED_RECEIPT_KINDS:
            raise ActionStoreError(
                "receipt kind %s is reserved for its transactional owner" % kind
            )
        now = float(self._clock())
        with self._transaction() as cursor:
            self._get_action_row(cursor, action_id)
            return self._insert_receipt(
                cursor,
                action_id,
                receipt_key,
                kind,
                status_value,
                evidence,
                external_id,
                observed_at,
                now,
            )

    def gate(
        self,
        action_id,
        evidence,
        receipt_key="owner-gate",
        actor="gate",
        external_id=None,
    ):
        """Attach the owner-approval gate receipt and move to ``gated``.

        The store records the evidence verbatim; it does NOT judge it.
        Judgment happens at point of use, inside
        :meth:`begin_owner_authorized_dispatch` (invariant I4).
        """

        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            if row["state"] == STATE_GATED:
                receipt = self._insert_receipt(
                    cursor,
                    action_id,
                    receipt_key,
                    GATE_RECEIPT_KIND,
                    "passed",
                    evidence,
                    external_id,
                    None,
                    now,
                )
                return self._action_dict(row), receipt
            if row["state"] != STATE_PROPOSED:
                raise ActionTransitionError("only proposed actions can be gated")
            receipt = self._insert_receipt(
                cursor,
                action_id,
                receipt_key,
                GATE_RECEIPT_KIND,
                "passed",
                evidence,
                external_id,
                None,
                now,
            )
            self._transition(
                cursor,
                row,
                STATE_GATED,
                actor,
                "gate_passed",
                {"receipt_key": receipt_key},
                now,
            )
            return self._action_dict(self._get_action_row(cursor, action_id)), receipt

    # --------------------------------------------------- leases & recovery

    @classmethod
    def _lease_scope(cls, *, source=None, source_prefix=None, action_type=None, action_ids=None):
        if source is not None and source_prefix is not None:
            raise ActionStoreError("source and source_prefix are mutually exclusive")
        clauses = []
        values = []
        if source is not None:
            clauses.append("source=?")
            values.append(cls._validate_text("source", source))
        if source_prefix is not None:
            prefix = cls._validate_text("source_prefix", source_prefix)
            clauses.append("substr(source,1,?)=?")
            values.extend((len(prefix), prefix))
        if action_type is not None:
            clauses.append("action_type=?")
            values.append(cls._validate_text("action_type", action_type))
        if action_ids is not None:
            ids = tuple(str(action_id) for action_id in action_ids)
            if not ids:
                clauses.append("0")
            else:
                clauses.append("action_id IN (%s)" % ",".join("?" for _ in ids))
                values.extend(ids)
        return (" AND " + " AND ".join(clauses) if clauses else ""), values

    def _dispatch_observation_recovery(self, cursor, row):
        """Return one valid immutable GET-only recovery contract, if present.

        Ordinary dispatched actions keep the conservative lease-reaper
        behavior (terminal ambiguity).  Only a contract inserted atomically
        by :meth:`begin_owner_authorized_dispatch` changes an expired
        dispatch into read-only reconciliation work.
        """

        receipts = cursor.execute(
            "SELECT * FROM receipts WHERE action_id=? AND kind=?",
            (row["action_id"], RECOVERY_RECEIPT_KIND),
        ).fetchall()
        if len(receipts) != 1:
            return None
        receipt = receipts[0]
        try:
            evidence = json.loads(receipt["evidence_json"])
            issued_at = float(evidence.get("issued_at"))
            deadline = float(evidence.get("observation_deadline"))
        except Exception:
            return None
        execution_digest = evidence.get("execution_digest")
        fields = {
            "schema",
            "version",
            "action_id",
            "action_digest",
            "execution_digest",
            "issued_at",
            "observation_deadline",
            "max_observations",
        }
        if (
            not isinstance(evidence, dict)
            or set(evidence) != fields
            or evidence.get("schema") != RECOVERY_SCHEMA
            or isinstance(evidence.get("version"), bool)
            or evidence.get("version") != 1
            or evidence.get("action_id") != row["action_id"]
            or not hmac.compare_digest(
                str(evidence.get("action_digest") or ""),
                str(row["payload_sha256"] or ""),
            )
            or not isinstance(execution_digest, str)
            or len(execution_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in execution_digest
            )
            or receipt["receipt_key"]
            != "dispatch-observation-recovery:" + execution_digest
            or receipt["status"] != "observation_only"
            or receipt["external_id"] != execution_digest
            or not hmac.compare_digest(
                str(receipt["evidence_sha256"] or ""), sha256_json_utf8(evidence)
            )
            or not math.isfinite(issued_at)
            or not math.isfinite(deadline)
            or issued_at <= 0
            or deadline <= issued_at
            or deadline - issued_at > 60 * 60
            or float(receipt["created_at"]) != issued_at
            or isinstance(evidence.get("max_observations"), bool)
            or not isinstance(evidence.get("max_observations"), int)
            or not 1 <= evidence["max_observations"] <= 100
        ):
            return None
        return evidence

    def _dispatch_observation_attempts(self, cursor, row, recovery):
        rows = cursor.execute(
            "SELECT * FROM receipts WHERE action_id=? AND kind=?",
            (row["action_id"], OBSERVATION_RECEIPT_KIND),
        ).fetchall()
        by_attempt = {}
        fields = {
            "schema",
            "version",
            "action_id",
            "action_digest",
            "execution_digest",
            "attempt",
            "started_at",
        }
        for receipt in rows:
            try:
                evidence = json.loads(receipt["evidence_json"])
                started_at = float(evidence.get("started_at"))
            except Exception as error:
                raise ActionTransitionError(
                    "dispatch observation journal is invalid"
                ) from error
            attempt = evidence.get("attempt")
            execution_digest = recovery["execution_digest"]
            if (
                not isinstance(evidence, dict)
                or set(evidence) != fields
                or evidence.get("schema") != OBSERVATION_SCHEMA
                or isinstance(evidence.get("version"), bool)
                or evidence.get("version") != 1
                or evidence.get("action_id") != row["action_id"]
                or not hmac.compare_digest(
                    str(evidence.get("action_digest") or ""),
                    str(row["payload_sha256"] or ""),
                )
                or evidence.get("execution_digest") != execution_digest
                or isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or not 1 <= attempt <= recovery["max_observations"]
                or receipt["receipt_key"]
                != "dispatch-observation:%s:%03d" % (execution_digest, attempt)
                or receipt["status"] != "started"
                or receipt["external_id"] != execution_digest
                or not hmac.compare_digest(
                    str(receipt["evidence_sha256"] or ""), sha256_json_utf8(evidence)
                )
                or not math.isfinite(started_at)
                or started_at < float(recovery["issued_at"])
                or started_at > float(recovery["observation_deadline"])
                or float(receipt["created_at"]) != started_at
                or attempt in by_attempt
            ):
                raise ActionTransitionError(
                    "dispatch observation journal is invalid"
                )
            by_attempt[attempt] = receipt
        if set(by_attempt) != set(range(1, len(by_attempt) + 1)):
            raise ActionTransitionError("dispatch observation journal is invalid")
        return [by_attempt[index] for index in range(1, len(by_attempt) + 1)]

    def _terminalize_dispatch_observation(
        self, cursor, row, recovery, now, *, actor, reason, observations
    ):
        message = (
            "governed action outcome remains explicitly ambiguous after "
            "bounded GET-only reconciliation"
        )
        result = {
            "status": "ambiguous",
            "effect_state": "unknown",
            "execution_digest": recovery["execution_digest"],
            "observation_attempts": int(observations),
            "reason": reason,
        }
        self._transition(
            cursor,
            row,
            STATE_FAILED,
            actor,
            "dispatch_observation_ambiguous",
            {
                "reason": reason,
                "observations": int(observations),
                "execution_digest": recovery["execution_digest"],
            },
            now,
            result=result,
            error=message,
            clear_lease=True,
        )
        self._dead_letter_action(cursor, row["action_id"], message, now)
        return self._action_dict(self._get_action_row(cursor, row["action_id"]))

    def _recover_expired_action_leases(
        self,
        cursor,
        now,
        *,
        source=None,
        source_prefix=None,
        action_type=None,
        action_ids=None,
    ):
        scope, scope_values = self._lease_scope(
            source=source,
            source_prefix=source_prefix,
            action_type=action_type,
            action_ids=action_ids,
        )
        dispatched = cursor.execute(
            """SELECT * FROM actions
               WHERE state=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?%s"""
            % scope,
            (STATE_DISPATCHED, now, *scope_values),
        ).fetchall()
        for row in dispatched:
            recovery = self._dispatch_observation_recovery(cursor, row)
            if recovery is not None:
                try:
                    observations = len(
                        self._dispatch_observation_attempts(cursor, row, recovery)
                    )
                except ActionTransitionError:
                    self._terminalize_dispatch_observation(
                        cursor,
                        row,
                        recovery,
                        now,
                        actor="lease-reaper",
                        reason="invalid_observation_journal",
                        observations=0,
                    )
                    continue
                if (
                    now >= float(recovery["observation_deadline"])
                    or observations >= int(recovery["max_observations"])
                ):
                    self._terminalize_dispatch_observation(
                        cursor,
                        row,
                        recovery,
                        now,
                        actor="lease-reaper",
                        reason="observation_bound_exhausted",
                        observations=observations,
                    )
                    continue
                cursor.execute(
                    """UPDATE actions
                       SET lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                       WHERE action_id=?""",
                    (now, row["action_id"]),
                )
                self._add_event(
                    cursor,
                    row["action_id"],
                    "dispatch_observation_lease_expired",
                    row["state"],
                    row["state"],
                    "lease-reaper",
                    {
                        "previous_owner": row["lease_owner"],
                        "mode": "get_only",
                        "observations": observations,
                    },
                    now,
                )
                continue
            reason = "dispatch lease expired; external outcome is ambiguous"
            self._transition(
                cursor,
                row,
                STATE_FAILED,
                "lease-reaper",
                "ambiguous_dispatch_expired",
                {"reason": reason, "previous_owner": row["lease_owner"]},
                now,
                error=reason,
                clear_lease=True,
            )
            self._dead_letter_action(cursor, row["action_id"], reason, now)

        recoverable = cursor.execute(
            """SELECT * FROM actions
               WHERE state IN (?,?,?) AND lease_expires_at IS NOT NULL
                 AND lease_expires_at<=?%s""" % scope,
            (
                STATE_GATED,
                STATE_ACCEPTED,
                STATE_VERIFIED,
                now,
                *scope_values,
            ),
        ).fetchall()
        for row in recoverable:
            cursor.execute(
                """UPDATE actions SET lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE action_id=?""",
                (now, row["action_id"]),
            )
            self._add_event(
                cursor,
                row["action_id"],
                "lease_expired",
                row["state"],
                row["state"],
                "lease-reaper",
                {"previous_owner": row["lease_owner"]},
                now,
            )

    def recover_expired_leases(
        self, *, source=None, source_prefix=None, action_type=None, action_ids=None
    ):
        now = float(self._clock())
        with self._transaction() as cursor:
            self._recover_expired_action_leases(
                cursor,
                now,
                source=source,
                source_prefix=source_prefix,
                action_type=action_type,
                action_ids=action_ids,
            )

    def lease_next(
        self,
        owner,
        *,
        lease_seconds=60.0,
        states=(STATE_GATED, STATE_ACCEPTED, STATE_VERIFIED),
        source=None,
        source_prefix=None,
        action_type=None,
        action_ids=None,
    ):
        owner = self._validate_text("owner", owner)
        lease_seconds = float(lease_seconds)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ActionStoreError("lease_seconds must be positive")
        states = tuple(str(state) for state in states)
        if not states or any(state not in _LEASEABLE_STATES for state in states):
            raise ActionStoreError(
                "only gated, accepted, or verified actions are leaseable"
            )
        if action_ids is not None:
            action_ids = tuple(str(action_id) for action_id in action_ids)
            if not action_ids:
                return None
        now = float(self._clock())
        state_placeholders = ",".join("?" for _ in states)
        action_filter, scoped_values = self._lease_scope(
            source=source,
            source_prefix=source_prefix,
            action_type=action_type,
            action_ids=action_ids,
        )
        values = list(states) + [now, now]
        values.extend(scoped_values)
        with self._transaction() as cursor:
            self._recover_expired_action_leases(
                cursor,
                now,
                source=source,
                source_prefix=source_prefix,
                action_type=action_type,
                action_ids=action_ids,
            )
            row = cursor.execute(
                """SELECT * FROM actions
                   WHERE state IN (%s) AND next_attempt_at<=?
                     AND (lease_owner IS NULL OR lease_expires_at<=?)%s
                   ORDER BY next_attempt_at, created_at, action_id LIMIT 1"""
                % (state_placeholders, action_filter),
                tuple(values),
            ).fetchone()
            if row is None:
                return None
            expires = now + lease_seconds
            cursor.execute(
                """UPDATE actions SET lease_owner=?,lease_expires_at=?,updated_at=?
                   WHERE action_id=?""",
                (owner, expires, now, row["action_id"]),
            )
            self._add_event(
                cursor,
                row["action_id"],
                "leased",
                row["state"],
                row["state"],
                owner,
                {"lease_expires_at": expires},
                now,
            )
            return self._action_dict(self._get_action_row(cursor, row["action_id"]))

    def lease_dispatched_observation(
        self,
        owner,
        *,
        lease_seconds=60.0,
        source=None,
        source_prefix=None,
        action_type=None,
        action_ids=None,
    ):
        """Lease only dispatched rows carrying a durable GET-only contract.

        This method can never make an ordinary dispatched row retryable and
        can never transition a row back to ``gated``.  Exhausted contracts
        are terminalized as explicitly ambiguous without another effect.
        """

        owner = self._validate_text("owner", owner)
        lease_seconds = float(lease_seconds)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ActionStoreError("lease_seconds must be positive")
        if action_ids is not None:
            action_ids = tuple(str(action_id) for action_id in action_ids)
            if not action_ids:
                return None
        now = float(self._clock())
        scope, scope_values = self._lease_scope(
            source=source,
            source_prefix=source_prefix,
            action_type=action_type,
            action_ids=action_ids,
        )
        with self._transaction() as cursor:
            self._recover_expired_action_leases(
                cursor,
                now,
                source=source,
                source_prefix=source_prefix,
                action_type=action_type,
                action_ids=action_ids,
            )
            rows = cursor.execute(
                """SELECT * FROM actions
                   WHERE state=? AND next_attempt_at<=?
                     AND (lease_owner IS NULL OR lease_expires_at<=?)%s
                   ORDER BY next_attempt_at,created_at,action_id""" % scope,
                (STATE_DISPATCHED, now, now, *scope_values),
            ).fetchall()
            for row in rows:
                recovery = self._dispatch_observation_recovery(cursor, row)
                if recovery is None:
                    continue
                try:
                    observations = len(
                        self._dispatch_observation_attempts(cursor, row, recovery)
                    )
                except ActionTransitionError:
                    self._terminalize_dispatch_observation(
                        cursor,
                        row,
                        recovery,
                        now,
                        actor=owner,
                        reason="invalid_observation_journal",
                        observations=0,
                    )
                    continue
                if (
                    now >= float(recovery["observation_deadline"])
                    or observations >= int(recovery["max_observations"])
                ):
                    self._terminalize_dispatch_observation(
                        cursor,
                        row,
                        recovery,
                        now,
                        actor=owner,
                        reason="observation_bound_exhausted",
                        observations=observations,
                    )
                    continue
                expires = now + lease_seconds
                cursor.execute(
                    """UPDATE actions SET lease_owner=?,lease_expires_at=?,updated_at=?
                       WHERE action_id=?""",
                    (owner, expires, now, row["action_id"]),
                )
                self._add_event(
                    cursor,
                    row["action_id"],
                    "dispatch_observation_leased",
                    row["state"],
                    row["state"],
                    owner,
                    {"lease_expires_at": expires, "mode": "get_only"},
                    now,
                )
                return self._action_dict(
                    self._get_action_row(cursor, row["action_id"])
                )
        return None

    # -------------------------------------------- owner-authorized dispatch

    def begin_owner_authorized_dispatch(
        self,
        action_id,
        owner,
        *,
        gate_receipt_key,
        expected_source,
        expected_source_ref,
        expected_action_type,
        expected_payload,
        expected_approval_id,
        expected_decision_id,
        expected_execution_digest,
        observation_window_seconds,
        max_observations,
        gate_validator,
    ):
        """Atomically consume one exact, still-live owner approval gate.

        Implements invariants I4 and I5: the caller-supplied
        ``gate_validator`` runs INSIDE this transaction against the durable
        receipts re-read here, the store re-checks the structural gate
        bindings itself, and the GET-only recovery contract is inserted in
        the same transaction as the ``gated -> dispatched`` transition.
        """

        gate_receipt_key = self._validate_text("gate_receipt_key", gate_receipt_key)
        expected_source = self._validate_text("expected_source", expected_source)
        expected_source_ref = self._validate_text(
            "expected_source_ref", expected_source_ref
        )
        expected_action_type = self._validate_text(
            "expected_action_type", expected_action_type
        )
        expected_approval_id = self._validate_text(
            "expected_approval_id", expected_approval_id
        )
        expected_decision_id = self._validate_text(
            "expected_decision_id", expected_decision_id
        )
        expected_execution_digest = self._validate_text(
            "expected_execution_digest", expected_execution_digest
        )
        if not callable(gate_validator):
            raise ActionStoreError("gate validator must be callable")
        try:
            observation_window = float(observation_window_seconds)
        except (TypeError, ValueError, OverflowError) as error:
            raise ActionStoreError("observation window is invalid") from error
        if (
            len(expected_execution_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_execution_digest
            )
            or not math.isfinite(observation_window)
            or not 1 <= observation_window <= 60 * 60
            or isinstance(max_observations, bool)
            or not isinstance(max_observations, int)
            or not 1 <= max_observations <= 100
        ):
            raise ActionStoreError("dispatch observation contract is invalid")
        expected_payload_json = canonical_json_utf8(expected_payload)
        expected_payload_digest = hashlib.sha256(
            expected_payload_json.encode("utf-8")
        ).hexdigest()
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] != STATE_GATED:
                raise ActionTransitionError("only gated actions can be dispatched")
            if float(row["next_attempt_at"]) > now:
                raise ActionTransitionError("action retry backoff has not elapsed")
            if (
                row["source"] != expected_source
                or row["source_ref"] != expected_source_ref
                or row["action_type"] != expected_action_type
                or row["payload_json"] != expected_payload_json
                or not hmac.compare_digest(
                    str(row["payload_sha256"] or ""), expected_payload_digest
                )
            ):
                raise ActionTransitionError(
                    "owner authorization does not bind this action payload"
                )
            gates = cursor.execute(
                "SELECT * FROM receipts WHERE action_id=? AND kind=?",
                (action_id, GATE_RECEIPT_KIND),
            ).fetchall()
            if len(gates) != 1 or gates[0]["receipt_key"] != gate_receipt_key:
                raise ActionTransitionError(
                    "exactly one owner authorization gate is required"
                )
            receipt = gates[0]
            # Structural in-transaction re-check: shape-agnostic bindings the
            # store enforces itself, in addition to the semantic validator.
            try:
                evidence = json.loads(receipt["evidence_json"])
                decided_at = float(evidence.get("decided_at_epoch"))
                expires_at = float(evidence.get("expires_at_epoch"))
            except Exception as error:
                raise ActionTransitionError(
                    "owner authorization receipt is invalid"
                ) from error
            if (
                not isinstance(evidence, dict)
                or receipt["status"] != "passed"
                or receipt["external_id"] != expected_approval_id
                or not hmac.compare_digest(
                    str(receipt["evidence_sha256"] or ""),
                    sha256_json_utf8(evidence),
                )
                or evidence.get("decision") != "approved"
                or evidence.get("authority") != "owner"
                or evidence.get("decision_id") != expected_decision_id
                or evidence.get("approval_id") != expected_approval_id
                or evidence.get("action_id") != action_id
                or not hmac.compare_digest(
                    str(evidence.get("action_digest") or ""),
                    expected_payload_digest,
                )
                or isinstance(evidence.get("revision"), bool)
                or evidence.get("revision") != 1
                or not math.isfinite(decided_at)
                or not math.isfinite(expires_at)
                or decided_at <= 0
                or decided_at > now + _GATE_CLOCK_SKEW_SECONDS
                or expires_at <= now
                or expires_at <= decided_at
            ):
                raise ActionTransitionError(
                    "owner authorization receipt does not bind this action"
                )
            # Backstop independent of validator and worker configuration: a
            # grant-bound receipt never dispatches a tool the catalog does
            # not explicitly allow on standing-grant authority.
            if evidence.get("binding_method") == GRANT_BINDING_METHOD:
                intent_value = (
                    expected_payload.get("intent")
                    if isinstance(expected_payload, dict)
                    else None
                )
                grant_tool_name = (
                    intent_value.get("tool_name")
                    if isinstance(intent_value, dict)
                    else None
                )
                if grant_tool_name not in GRANT_AUTHORIZABLE_TOOL_NAMES:
                    raise ActionTransitionError(
                        "bounded grant authority never covers this tool"
                    )
            # THE in-transaction semantic re-validation (invariant I4): the
            # validator sees the durable projections re-read in THIS
            # transaction and the store's own clock — never the caller's
            # earlier view.
            action_projection = self._action_dict(row)
            receipt_projections = [
                self._receipt_dict(item)
                for item in cursor.execute(
                    "SELECT * FROM receipts WHERE action_id=? "
                    "ORDER BY created_at,receipt_id",
                    (action_id,),
                ).fetchall()
            ]
            try:
                authorization = gate_validator(
                    action_projection, receipt_projections, now
                )
            except ActionStoreError:
                raise
            except Exception as error:
                raise ActionTransitionError(
                    "owner gate validator rejected this action"
                ) from error
            if (
                not isinstance(authorization, GateAuthorization)
                or authorization.expired
                or authorization.receipt_key != gate_receipt_key
                or authorization.approval_id != expected_approval_id
                or authorization.decision_id != expected_decision_id
            ):
                raise ActionTransitionError(
                    "owner gate validator did not authorize this dispatch"
                )
            existing_recovery = cursor.execute(
                "SELECT 1 FROM receipts WHERE action_id=? AND kind=?",
                (action_id, RECOVERY_RECEIPT_KIND),
            ).fetchone()
            if existing_recovery is not None:
                raise ActionTransitionError(
                    "dispatch observation contract already exists"
                )
            recovery = {
                "schema": RECOVERY_SCHEMA,
                "version": 1,
                "action_id": action_id,
                "action_digest": expected_payload_digest,
                "execution_digest": expected_execution_digest,
                "issued_at": now,
                "observation_deadline": now + observation_window,
                "max_observations": max_observations,
            }
            self._insert_receipt(
                cursor,
                action_id,
                "dispatch-observation-recovery:" + expected_execution_digest,
                RECOVERY_RECEIPT_KIND,
                "observation_only",
                recovery,
                expected_execution_digest,
                now,
                now,
            )
            attempt = int(row["attempt_count"]) + 1
            if attempt > int(row["max_attempts"]):
                raise ActionTransitionError("action attempt budget is exhausted")
            self._check_transition(row["state"], STATE_DISPATCHED)
            cursor.execute(
                """UPDATE actions SET state=?,attempt_count=?,last_error=NULL,updated_at=?
                   WHERE action_id=?""",
                (STATE_DISPATCHED, attempt, now, row["action_id"]),
            )
            self._add_event(
                cursor,
                row["action_id"],
                "dispatched",
                row["state"],
                STATE_DISPATCHED,
                owner,
                {"attempt": attempt},
                now,
            )
            return self._action_dict(self._get_action_row(cursor, action_id))

    # ------------------------------------------------ dispatched observation

    def begin_dispatched_observation(self, action_id, owner, execution_digest):
        """Durably consume one bounded GET attempt before external observation."""

        execution_digest = self._validate_text("execution_digest", execution_digest)
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] != STATE_DISPATCHED:
                raise ActionTransitionError(
                    "only dispatched actions can begin effect observation"
                )
            recovery = self._dispatch_observation_recovery(cursor, row)
            if (
                recovery is None
                or not hmac.compare_digest(
                    str(recovery["execution_digest"]), execution_digest
                )
            ):
                raise ActionTransitionError(
                    "dispatch observation contract does not bind this action"
                )
            try:
                observations = self._dispatch_observation_attempts(
                    cursor, row, recovery
                )
            except ActionTransitionError:
                action = self._terminalize_dispatch_observation(
                    cursor,
                    row,
                    recovery,
                    now,
                    actor=owner,
                    reason="invalid_observation_journal",
                    observations=0,
                )
                return action, None
            if (
                now >= float(recovery["observation_deadline"])
                or len(observations) >= int(recovery["max_observations"])
            ):
                action = self._terminalize_dispatch_observation(
                    cursor,
                    row,
                    recovery,
                    now,
                    actor=owner,
                    reason="observation_bound_exhausted",
                    observations=len(observations),
                )
                return action, None
            attempt = len(observations) + 1
            evidence = {
                "schema": OBSERVATION_SCHEMA,
                "version": 1,
                "action_id": row["action_id"],
                "action_digest": row["payload_sha256"],
                "execution_digest": execution_digest,
                "attempt": attempt,
                "started_at": now,
            }
            receipt_key = "dispatch-observation:%s:%03d" % (
                execution_digest,
                attempt,
            )
            receipt = self._insert_receipt(
                cursor,
                row["action_id"],
                receipt_key,
                OBSERVATION_RECEIPT_KIND,
                "started",
                evidence,
                execution_digest,
                now,
                now,
            )
            self._add_event(
                cursor,
                row["action_id"],
                "dispatch_observation_started",
                row["state"],
                row["state"],
                owner,
                {"attempt": attempt, "mode": "get_only"},
                now,
            )
            return self._action_dict(row), receipt

    def defer_dispatched_observation(
        self,
        action_id,
        owner,
        execution_digest,
        observation_receipt_key,
        reason,
        delay_seconds,
    ):
        """Record an unresolved GET and either defer or end as ambiguous."""

        execution_digest = self._validate_text("execution_digest", execution_digest)
        observation_receipt_key = self._validate_text(
            "observation_receipt_key", observation_receipt_key
        )
        reason = self._validate_text("reason", str(reason))[:2000]
        delay = float(delay_seconds)
        if not math.isfinite(delay) or not 0 < delay <= 24 * 60 * 60:
            raise ActionStoreError("defer delay must be between zero and one day")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] != STATE_DISPATCHED:
                raise ActionTransitionError(
                    "only dispatched observations can be deferred"
                )
            recovery = self._dispatch_observation_recovery(cursor, row)
            if (
                recovery is None
                or not hmac.compare_digest(
                    str(recovery["execution_digest"]), execution_digest
                )
            ):
                raise ActionTransitionError(
                    "dispatch observation contract does not bind this action"
                )
            observations = self._dispatch_observation_attempts(cursor, row, recovery)
            if (
                not observations
                or observations[-1]["receipt_key"] != observation_receipt_key
            ):
                raise ActionTransitionError(
                    "dispatch observation attempt is not the current attempt"
                )
            if (
                now >= float(recovery["observation_deadline"])
                or len(observations) >= int(recovery["max_observations"])
            ):
                return self._terminalize_dispatch_observation(
                    cursor,
                    row,
                    recovery,
                    now,
                    actor=owner,
                    reason="observation_bound_exhausted",
                    observations=len(observations),
                )
            next_at = now + delay
            message = "governed dispatch observation unresolved (%d/%d)" % (
                len(observations),
                recovery["max_observations"],
            )
            cursor.execute(
                """UPDATE actions SET next_attempt_at=?,lease_owner=NULL,
                   lease_expires_at=NULL,last_error=?,updated_at=? WHERE action_id=?""",
                (next_at, message, now, action_id),
            )
            self._add_event(
                cursor,
                action_id,
                "dispatch_observation_deferred",
                row["state"],
                row["state"],
                owner,
                {
                    "reason": reason,
                    "retry_at": next_at,
                    "observation": len(observations),
                    "mode": "get_only",
                },
                now,
            )
            return self._action_dict(self._get_action_row(cursor, action_id))

    # ------------------------------------------------------- forward states

    def defer_leased(self, action_id, owner, reason, delay_seconds, *, event_type="deferred"):
        """Release a lease without changing lifecycle state or effect count.

        Only for work safe to repeat in its current state: a gated action
        whose gate is still unconsumed, or read-only verification after an
        external mutation produced immutable acceptance evidence.
        """

        reason = self._validate_text("reason", str(reason))[:2000]
        delay = float(delay_seconds)
        if not math.isfinite(delay) or not 0 < delay <= 24 * 60 * 60:
            raise ActionStoreError("defer delay must be between zero and one day")
        event_type = self._validate_text("event_type", event_type)
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] not in (STATE_GATED, STATE_ACCEPTED, STATE_VERIFIED):
                raise ActionTransitionError("action state cannot be deferred safely")
            next_at = now + delay
            cursor.execute(
                """UPDATE actions SET next_attempt_at=?,lease_owner=NULL,
                   lease_expires_at=NULL,last_error=?,updated_at=? WHERE action_id=?""",
                (next_at, reason, now, action_id),
            )
            self._add_event(
                cursor,
                action_id,
                event_type,
                row["state"],
                row["state"],
                owner,
                {"reason": reason, "retry_at": next_at},
                now,
            )
            return self._action_dict(self._get_action_row(cursor, action_id))

    def accept(
        self,
        action_id,
        owner,
        receipt_key,
        kind,
        evidence,
        *,
        external_id=None,
        result=None,
    ):
        kind = self._validate_text("kind", kind)
        if kind in _RESERVED_RECEIPT_KINDS:
            raise ActionStoreError(
                "receipt kind %s is reserved for its transactional owner" % kind
            )
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] != STATE_DISPATCHED:
                raise ActionTransitionError("only dispatched actions can be accepted")
            receipt = self._insert_receipt(
                cursor,
                action_id,
                receipt_key,
                kind,
                "accepted",
                evidence,
                external_id,
                None,
                now,
            )
            self._transition(
                cursor,
                row,
                STATE_ACCEPTED,
                owner,
                "accepted",
                {"receipt_key": receipt_key, "kind": kind},
                now,
                result=result,
            )
            return self._action_dict(self._get_action_row(cursor, action_id)), receipt

    def verify(
        self,
        action_id,
        owner,
        receipt_key,
        evidence,
        *,
        qualifying_receipt_keys,
        kind="verification",
    ):
        kind = self._validate_text("kind", kind)
        if kind in _RESERVED_RECEIPT_KINDS:
            raise ActionStoreError(
                "receipt kind %s is reserved for its transactional owner" % kind
            )
        qualifying_receipt_keys = tuple(qualifying_receipt_keys or ())
        if not qualifying_receipt_keys:
            raise ActionStoreError(
                "verification requires at least one qualifying receipt"
            )
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] != STATE_ACCEPTED:
                raise ActionTransitionError("only accepted actions can be verified")
            placeholders = ",".join("?" for _ in qualifying_receipt_keys)
            found = cursor.execute(
                "SELECT receipt_key FROM receipts WHERE action_id=? AND receipt_key IN (%s)"
                % placeholders,
                (action_id,) + qualifying_receipt_keys,
            ).fetchall()
            found_keys = {item["receipt_key"] for item in found}
            missing = set(qualifying_receipt_keys) - found_keys
            if missing:
                raise ActionNotFound(
                    "qualifying receipts not found: %s" % sorted(missing)
                )
            receipt = self._insert_receipt(
                cursor,
                action_id,
                receipt_key,
                kind,
                "verified",
                evidence,
                None,
                None,
                now,
            )
            self._transition(
                cursor,
                row,
                STATE_VERIFIED,
                owner,
                "verified",
                {
                    "receipt_key": receipt_key,
                    "qualifying_receipt_keys": list(qualifying_receipt_keys),
                },
                now,
            )
            return self._action_dict(self._get_action_row(cursor, action_id)), receipt

    def complete(self, action_id, owner, result):
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            if row["state"] != STATE_VERIFIED:
                raise ActionTransitionError(
                    "an action must be verified before completion"
                )
            self._transition(
                cursor,
                row,
                STATE_COMPLETED,
                owner,
                "completed",
                {"result_sha256": sha256_json_utf8(result)},
                now,
                result=result,
                clear_lease=True,
            )
            return self._action_dict(self._get_action_row(cursor, action_id))

    def fail_attempt(self, action_id, owner, error, retryable):
        """Terminalize as failed.  ``retryable`` is recorded, never honored
        with a second mutation (invariants I6/I10)."""

        error = self._validate_text("error", str(error))[:2000]
        now = float(self._clock())
        with self._transaction() as cursor:
            row = self._get_action_row(cursor, action_id)
            self._require_lease(row, owner, now)
            state = row["state"]
            if state not in (
                STATE_GATED,
                STATE_DISPATCHED,
                STATE_ACCEPTED,
                STATE_VERIFIED,
            ):
                raise ActionTransitionError("action in %s cannot fail" % state)
            reason = error
            if retryable and state != STATE_DISPATCHED:
                reason = "%s (not retried after %s: outcome may be ambiguous)" % (
                    error,
                    state,
                )
            elif retryable:
                reason = "%s (governed actions are never re-dispatched)" % error
            self._transition(
                cursor,
                row,
                STATE_FAILED,
                owner,
                "failed",
                {
                    "error": reason,
                    "attempt": row["attempt_count"],
                    "retryable_requested": bool(retryable),
                },
                now,
                error=reason,
                clear_lease=True,
            )
            self._dead_letter_action(cursor, action_id, reason, now)
            return self._action_dict(self._get_action_row(cursor, action_id))

    # ------------------------------------------------------------ read side

    def get_action(self, action_id):
        with self._lock:
            if self._conn is None:
                raise ActionStoreError("action store is closed")
            row = self._conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise ActionNotFound("action %s not found" % action_id)
            return self._action_dict(row)

    def list_receipts(self, action_id):
        with self._lock:
            if self._conn is None:
                raise ActionStoreError("action store is closed")
            rows = self._conn.execute(
                "SELECT * FROM receipts WHERE action_id=? ORDER BY created_at,receipt_id",
                (action_id,),
            ).fetchall()
            return [self._receipt_dict(row) for row in rows]

    def list_events(self, action_id):
        with self._lock:
            if self._conn is None:
                raise ActionStoreError("action store is closed")
            rows = self._conn.execute(
                "SELECT * FROM action_events WHERE action_id=? ORDER BY event_id",
                (action_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["details"] = self._json_load(item.pop("details_json"))
                result.append(item)
            return result

    def list_dead_letters(self):
        with self._lock:
            if self._conn is None:
                raise ActionStoreError("action store is closed")
            rows = self._conn.execute(
                "SELECT * FROM dead_letters ORDER BY created_at,dead_letter_id"
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["action_snapshot"] = self._json_load(
                    item.pop("action_snapshot_json")
                )
                result.append(item)
            return result


__all__ = (
    "OBSERVATION_SCHEMA",
    "RECOVERY_SCHEMA",
    "SqliteActionStore",
)
