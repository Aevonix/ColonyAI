"""Durable P8 visibility envelopes with authorized bounded projection.

The ledger persists :class:`FactVisibilityV1` records, never fact content.  A
producer must append a :class:`FactCandidateV1`, which lets this boundary
recompute the exact UTF-8 content digest before storing the envelope.  A fact
reference is an immutable event identity: exact replay is a no-op and any
changed content, audience, freshness, evidence, or confidence is a conflict.

This module has no server startup hook.  ``open_visibility_envelope_store()``
defaults disabled so merely importing or probing the P8 candidate creates no
directory or database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Optional

from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactVisibilityV1,
    ViewerContextV1,
    content_digest,
)


SCHEMA_VERSION = 1
MAX_PROJECTED_ENVELOPES = 64
MAX_ENVELOPES_SCANNED = 2_048

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")


class VisibilityEnvelopeConflictError(ValueError):
    """A fact reference replay changed its immutable visibility envelope."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _iso(value: datetime | str, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class VisibilityEnvelopeAppendV1:
    envelope: FactVisibilityV1
    sequence: int
    appended: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class VisibilityEnvelopeProjectionV1:
    envelopes: tuple[FactVisibilityV1, ...]
    viewer_attested: bool
    corrupt_count: int
    truncated: bool
    viewer_digest: str
    audit_digest: str
    schema_version: int = SCHEMA_VERSION

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "envelopes": [envelope.public() for envelope in self.envelopes],
            "viewer_attested": self.viewer_attested,
            "corrupt_count": self.corrupt_count,
            "truncated": self.truncated,
            "viewer_digest": self.viewer_digest,
            "audit_digest": self.audit_digest,
        }


class FactVisibilityStore:
    """Append-only SQLite ledger of verified visibility envelopes."""

    def __init__(self, db_path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = str(db_path)
        if self.path != ":memory:":
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch(mode=0o600)
            else:
                os.chmod(path, 0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA recursive_triggers=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fact_visibility_envelopes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_ref TEXT NOT NULL UNIQUE,
                visibility_digest TEXT NOT NULL UNIQUE,
                content_digest TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                subject_person_id TEXT NOT NULL,
                viewer_scope TEXT NOT NULL,
                shareability TEXT NOT NULL,
                confidence REAL NOT NULL,
                observed_at TEXT NOT NULL,
                fresh_until TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                stored_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fact_visibility_viewer_fresh
                ON fact_visibility_envelopes(viewer_scope,fresh_until);
            CREATE INDEX IF NOT EXISTS idx_fact_visibility_subject_fresh
                ON fact_visibility_envelopes(subject_person_id,fresh_until);
            CREATE INDEX IF NOT EXISTS idx_fact_visibility_shareability_fresh
                ON fact_visibility_envelopes(shareability,fresh_until);
            CREATE INDEX IF NOT EXISTS idx_fact_visibility_fresh
                ON fact_visibility_envelopes(fresh_until);
            CREATE TRIGGER IF NOT EXISTS fact_visibility_no_update
                BEFORE UPDATE ON fact_visibility_envelopes BEGIN
                    SELECT RAISE(ABORT, 'fact visibility is append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS fact_visibility_no_delete
                BEFORE DELETE ON fact_visibility_envelopes BEGIN
                    SELECT RAISE(ABORT, 'fact visibility is append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS fact_visibility_no_replace
                BEFORE INSERT ON fact_visibility_envelopes
                WHEN EXISTS (
                    SELECT 1 FROM fact_visibility_envelopes
                    WHERE seq=NEW.seq
                       OR fact_ref=NEW.fact_ref
                       OR visibility_digest=NEW.visibility_digest
                ) BEGIN
                    SELECT RAISE(ABORT, 'fact visibility is append-only');
                END;
            """
        )
        self._conn.commit()
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    @staticmethod
    def _decode(row: sqlite3.Row) -> FactVisibilityV1:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("visibility envelope payload is unreadable") from exc
        envelope = FactVisibilityV1(**payload)
        indexed = {
            "fact_ref": str(row["fact_ref"]),
            "visibility_digest": str(row["visibility_digest"]),
            "content_digest": str(row["content_digest"]),
            "source_ref": str(row["source_ref"]),
            "subject_person_id": str(row["subject_person_id"]),
            "viewer_scope": str(row["viewer_scope"]),
            "shareability": str(row["shareability"]),
            "confidence": float(row["confidence"]),
            "observed_at": str(row["observed_at"]),
            "fresh_until": str(row["fresh_until"]),
        }
        expected = {
            "fact_ref": envelope.fact_ref,
            "visibility_digest": envelope.audit_digest,
            "content_digest": envelope.content_digest,
            "source_ref": envelope.source_ref,
            "subject_person_id": envelope.subject_person_id,
            "viewer_scope": envelope.viewer_scope,
            "shareability": envelope.shareability,
            "confidence": envelope.confidence,
            "observed_at": envelope.observed_at,
            "fresh_until": envelope.fresh_until,
        }
        if indexed != expected:
            raise ValueError("visibility envelope digest/index mismatch")
        return envelope

    def append(self, candidate: FactCandidateV1) -> VisibilityEnvelopeAppendV1:
        """Verify content binding, then append or exactly replay one envelope."""

        if not isinstance(candidate, FactCandidateV1):
            raise ValueError("FactVisibilityStore accepts FactCandidateV1 only")
        envelope = candidate.visibility
        if content_digest(candidate.content) != envelope.content_digest:
            raise ValueError("fact content digest does not match visibility")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM fact_visibility_envelopes WHERE fact_ref=?",
                    (envelope.fact_ref,),
                ).fetchone()
                if row is not None:
                    saved = self._decode(row)
                    if saved.audit_digest == envelope.audit_digest:
                        self._conn.commit()
                        return VisibilityEnvelopeAppendV1(
                            envelope=saved,
                            sequence=int(row["seq"]),
                            appended=False,
                            replayed=True,
                        )
                    raise VisibilityEnvelopeConflictError(
                        "fact reference changed its immutable visibility envelope")
                cursor = self._conn.execute(
                    "INSERT INTO fact_visibility_envelopes "
                    "(fact_ref,visibility_digest,content_digest,source_ref,"
                    "subject_person_id,viewer_scope,shareability,confidence,"
                    "observed_at,fresh_until,payload_json,stored_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        envelope.fact_ref,
                        envelope.audit_digest,
                        envelope.content_digest,
                        envelope.source_ref,
                        envelope.subject_person_id,
                        envelope.viewer_scope,
                        envelope.shareability,
                        envelope.confidence,
                        envelope.observed_at,
                        envelope.fresh_until,
                        _canonical(envelope.public()),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._conn.commit()
                return VisibilityEnvelopeAppendV1(
                    envelope=envelope,
                    sequence=int(cursor.lastrowid),
                    appended=True,
                    replayed=False,
                )
            except Exception:
                self._conn.rollback()
                raise

    def get(self, fact_ref: str) -> Optional[FactVisibilityV1]:
        normalized = str(fact_ref or "").strip()
        if not _REF_RE.fullmatch(normalized):
            raise ValueError("fact_ref is not a bounded opaque reference")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM fact_visibility_envelopes WHERE fact_ref=?",
                (normalized,),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def envelope_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM fact_visibility_envelopes"
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _viewer_scopes(viewer: ViewerContextV1) -> tuple[str, ...]:
        scopes = {"public", f"person:{viewer.viewer_person_id}"}
        if "global" in viewer.audiences:
            scopes.add("audience:global")
        if "shared" in viewer.audiences:
            scopes.add("audience:shared")
        if viewer.conversation_scope:
            scopes.add(f"conversation:{viewer.conversation_scope}")
        return tuple(sorted(scopes))

    def project_authorized(
        self,
        viewer: ViewerContextV1,
        *,
        now: datetime | str,
        min_confidence: float = 0.0,
        max_envelopes: int = 24,
    ) -> VisibilityEnvelopeProjectionV1:
        """Return only fresh envelopes authorized for the exact viewer."""

        if not isinstance(viewer, ViewerContextV1):
            raise ValueError("viewer must be ViewerContextV1")
        if not 1 <= int(max_envelopes) <= MAX_PROJECTED_ENVELOPES:
            raise ValueError("max_envelopes is outside bounded projection range")
        floor = float(min_confidence)
        if not math.isfinite(floor) or not 0.0 <= floor <= 1.0:
            raise ValueError("min_confidence must be between zero and one")
        observed = _iso(now, field="now")

        if not viewer.attested or not viewer.viewer_person_id:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "viewer_digest": viewer.audit_digest,
                "viewer_attested": False,
                "envelope_digests": (),
                "corrupt_count": 0,
                "truncated": False,
                "now": observed,
                "min_confidence": floor,
                "max_envelopes": int(max_envelopes),
            }
            return VisibilityEnvelopeProjectionV1(
                envelopes=(),
                viewer_attested=False,
                corrupt_count=0,
                truncated=False,
                viewer_digest=viewer.audit_digest,
                audit_digest=_digest(payload),
            )

        params: list[Any] = [observed, observed, floor]
        where = (
            "observed_at<=? AND fresh_until>? AND confidence>=?"
        )
        if viewer.viewer_person_id != viewer.owner_person_id:
            scopes = self._viewer_scopes(viewer)
            where += " AND viewer_scope IN (%s)" % ",".join(
                "?" for _ in scopes)
            params.extend(scopes)
        params.append(MAX_ENVELOPES_SCANNED + 1)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fact_visibility_envelopes WHERE " + where
                + " ORDER BY confidence DESC,observed_at DESC,fact_ref ASC LIMIT ?",
                tuple(params),
            ).fetchall()

        scan_truncated = len(rows) > MAX_ENVELOPES_SCANNED
        rows = rows[:MAX_ENVELOPES_SCANNED]
        eligible: list[FactVisibilityV1] = []
        corrupt_count = 0
        for row in rows:
            try:
                envelope = self._decode(row)
                decision = envelope.decision(
                    viewer, now=observed, min_confidence=floor)
            except Exception:
                corrupt_count += 1
                continue
            if decision.allowed:
                eligible.append(envelope)

        truncated = scan_truncated or len(eligible) > int(max_envelopes)
        projected = tuple(eligible[:int(max_envelopes)])
        payload = {
            "schema_version": SCHEMA_VERSION,
            "viewer_digest": viewer.audit_digest,
            "viewer_attested": True,
            "envelope_digests": tuple(
                envelope.audit_digest for envelope in projected),
            "corrupt_count": corrupt_count,
            "truncated": truncated,
            "now": observed,
            "min_confidence": floor,
            "max_envelopes": int(max_envelopes),
        }
        return VisibilityEnvelopeProjectionV1(
            envelopes=projected,
            viewer_attested=True,
            corrupt_count=corrupt_count,
            truncated=truncated,
            viewer_digest=viewer.audit_digest,
            audit_digest=_digest(payload),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def open_visibility_envelope_store(
    db_path: str | os.PathLike[str],
    *,
    enabled: bool = False,
) -> Optional[FactVisibilityStore]:
    """Create the store only after a caller explicitly enables this slice."""

    return FactVisibilityStore(db_path) if enabled is True else None


__all__ = [
    "FactVisibilityStore",
    "VisibilityEnvelopeAppendV1",
    "VisibilityEnvelopeConflictError",
    "VisibilityEnvelopeProjectionV1",
    "open_visibility_envelope_store",
]
