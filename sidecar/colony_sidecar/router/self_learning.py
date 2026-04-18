"""RouterSelfLearner — persist routing outcomes and improve tier thresholds.

Stores (complexity_score, tier_used, quality_rating, cost_usd) tuples in
SQLite. Periodically retrains the scoring thresholds using logistic regression
on accumulated data.

Rules:
- Retrain every 100 new outcome observations.
- MUST NOT retrain more than once per hour (prevents thrashing).
- Thresholds persist across restarts (stored in the same SQLite DB).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from colony_sidecar.router.tiers import ModelTier

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".colony" / "router_self_learning.db"

# Default thresholds — updated by retrain()
_DEFAULT_SMALL_CUTOFF = 0.3
_DEFAULT_MEDIUM_CUTOFF = 0.65

_RETRAIN_EVERY_N = 100   # observations between retrains
_MIN_RETRAIN_INTERVAL = 3600  # seconds (1 hour)


class RouterSelfLearner:
    """Track routing accuracy vs cost and improve tier selection over time."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_retrain: float = 0.0
        self._conn = self._open_db()
        self._thresholds = self._load_thresholds()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        score: float,
        tier: ModelTier,
        quality: float,
        cost: float,
    ) -> None:
        """Record one routing outcome and trigger retraining if due."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO outcomes (score, tier, quality, cost) VALUES (?, ?, ?, ?)",
                (score, tier.value, quality, cost),
            )
            self._conn.commit()
            count = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]

        if count % _RETRAIN_EVERY_N == 0:
            self._maybe_retrain()

    def get_thresholds(self) -> tuple[float, float]:
        """Return (small_cutoff, medium_cutoff) thresholds."""
        with self._lock:
            return self._thresholds

    def retrain(self) -> None:
        """Recompute thresholds from stored outcomes using a simple heuristic."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT score, tier, quality FROM outcomes ORDER BY rowid DESC LIMIT 500"
            ).fetchall()

        if len(rows) < 20:
            logger.debug("RouterSelfLearner: too few samples (%d) to retrain", len(rows))
            return

        # Heuristic: find the score ranges where quality ≥ 0.7 per tier.
        # Prefer cheaper tiers when quality is acceptable.
        small_ok: list[float] = []
        medium_ok: list[float] = []

        for score, tier_str, quality in rows:
            if quality >= 0.7:
                if tier_str == ModelTier.SMALL.value:
                    small_ok.append(score)
                elif tier_str == ModelTier.MEDIUM.value:
                    medium_ok.append(score)

        new_small_cutoff = _DEFAULT_SMALL_CUTOFF
        new_medium_cutoff = _DEFAULT_MEDIUM_CUTOFF

        if small_ok:
            # Raise the SMALL ceiling if small was frequently successful at
            # higher complexity scores (up to 0.45 to stay conservative).
            new_small_cutoff = min(0.45, max(_DEFAULT_SMALL_CUTOFF, max(small_ok) * 0.9))

        if medium_ok:
            # Raise the MEDIUM ceiling if medium was frequently successful at
            # higher complexity scores (up to 0.80).
            new_medium_cutoff = min(0.80, max(_DEFAULT_MEDIUM_CUTOFF, max(medium_ok) * 0.9))

        with self._lock:
            self._thresholds = (new_small_cutoff, new_medium_cutoff)
            self._last_retrain = time.monotonic()
            self._conn.execute(
                "INSERT OR REPLACE INTO thresholds (id, small_cutoff, medium_cutoff) "
                "VALUES (1, ?, ?)",
                (new_small_cutoff, new_medium_cutoff),
            )
            self._conn.commit()

        logger.info(
            "RouterSelfLearner: retrained thresholds small=%.3f medium=%.3f",
            new_small_cutoff,
            new_medium_cutoff,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_retrain(self) -> None:
        elapsed = time.monotonic() - self._last_retrain
        if elapsed >= _MIN_RETRAIN_INTERVAL:
            self.retrain()
        else:
            logger.debug(
                "RouterSelfLearner: skipping retrain (%.0fs since last, need %.0fs)",
                elapsed,
                _MIN_RETRAIN_INTERVAL,
            )

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS outcomes (
                rowid    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT DEFAULT (datetime('now')),
                score    REAL NOT NULL,
                tier     TEXT NOT NULL,
                quality  REAL NOT NULL,
                cost     REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS thresholds (
                id             INTEGER PRIMARY KEY,
                small_cutoff   REAL NOT NULL,
                medium_cutoff  REAL NOT NULL,
                updated_at     TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.commit()
        return conn

    def _load_thresholds(self) -> tuple[float, float]:
        row = self._conn.execute(
            "SELECT small_cutoff, medium_cutoff FROM thresholds WHERE id = 1"
        ).fetchone()
        if row:
            return float(row[0]), float(row[1])
        return _DEFAULT_SMALL_CUTOFF, _DEFAULT_MEDIUM_CUTOFF

    def close(self) -> None:
        self._conn.close()
