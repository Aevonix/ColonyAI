"""Short-lived SQLite stores must own and close every connection."""

import sqlite3

from colony_sidecar.autonomy.scheduler import ScheduleStore
from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter
from colony_sidecar.intelligence.synthesis.insight_store import InsightStore


def _track_connections(monkeypatch, module):
    original_connect = module.sqlite3.connect
    opened = []

    class TrackedConnection(sqlite3.Connection):
        explicitly_closed = False

        def close(self):
            self.explicitly_closed = True
            return super().close()

    def tracked_connect(*args, **kwargs):
        kwargs["factory"] = TrackedConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", tracked_connect)
    return opened


def test_insight_store_closes_transient_connections(tmp_path, monkeypatch):
    import colony_sidecar.intelligence.synthesis.insight_store as module

    opened = _track_connections(monkeypatch, module)
    store = InsightStore(tmp_path / "insights.db")
    assert store.list_dismissed() == set()
    assert opened
    assert all(connection.explicitly_closed for connection in opened)


def test_schedule_store_closes_transient_connections(tmp_path, monkeypatch):
    import colony_sidecar.autonomy.scheduler as module

    opened = _track_connections(monkeypatch, module)
    ScheduleStore(str(tmp_path / "schedules.db"))
    assert opened
    assert all(connection.explicitly_closed for connection in opened)


def test_delivery_limiter_closes_transient_connections(tmp_path, monkeypatch):
    import colony_sidecar.delivery.rate_limiter as module

    opened = _track_connections(monkeypatch, module)
    limiter = DeliveryRateLimiter(
        db_path=tmp_path / "delivery.db",
        quiet_start_hour=0,
        quiet_end_hour=0,
    )
    limiter.record_delivery("person-owner")
    assert opened
    assert all(connection.explicitly_closed for connection in opened)

