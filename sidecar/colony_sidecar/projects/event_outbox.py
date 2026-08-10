"""Crash-recoverable ProjectStore to host-journal projection.

The project ledger and host event journal are separate durable stores.  A
ProjectStore transaction therefore stages immutable event data in its own
outbox first.  This projector uses the journal's keyed append handshake, then
records the returned sequence in the project database.  Replaying after a
crash can finish either side without creating a second host event.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Mapping, Optional

from colony_sidecar.events.journal import event_record_request_digest
from colony_sidecar.projects.store import ProjectStore


_RECEIPT_ACKNOWLEDGEMENT_FIELDS = frozenset({
    "expected_seq",
    "expected_event_id",
    "expected_recorded_at",
    "expected_request_digest",
})


class ProjectEventProjector:
    """Drain immutable project events into the canonical host journal."""

    def __init__(
        self,
        store: ProjectStore,
        *,
        journal_projector: Optional[Callable[..., Optional[Mapping[str, Any]]]] = None,
        journal_acknowledger: Optional[Callable[..., bool]] = None,
    ) -> None:
        self.store = store
        if journal_projector is None:
            from colony_sidecar.events.journal import append_event_record

            journal_projector = append_event_record
        if journal_acknowledger is None:
            from colony_sidecar.events.journal import acknowledge_event_record

            journal_acknowledger = acknowledge_event_record
        self._project = journal_projector
        self._acknowledge = journal_acknowledger
        self._acknowledge_with_receipt = self._supports_receipt_acknowledgement(
            journal_acknowledger
        )
        if not self._acknowledge_with_receipt:
            raise TypeError(
                "project journal acknowledger must accept the exact projection "
                "receipt fields"
            )

    @staticmethod
    def _supports_receipt_acknowledgement(callback: Callable[..., bool]) -> bool:
        """Require an acknowledger that validates the exact journal receipt."""

        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            return False
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return True
        return _RECEIPT_ACKNOWLEDGEMENT_FIELDS.issubset(parameters)

    def _validated_payload(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        payload_error = str(row.get("payload_error") or "")
        payload = row.get("payload")
        if payload_error:
            raise ValueError(payload_error)
        if not isinstance(payload, dict):
            raise ValueError("project outbox payload is not an object")
        expected = self.store.project_event_envelope_digest(
            event_key=str(row.get("event_key") or ""),
            event_type=str(row.get("event_type") or ""),
            occurred_at=str(row.get("occurred_at") or ""),
            payload=payload,
        )
        if expected != str(row.get("event_digest") or ""):
            raise ValueError("project outbox envelope digest mismatch")
        return payload

    def _validated_projection_receipt(
        self,
        row: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if row.get("state") != "projected":
            raise ValueError("project outbox event is not projected")
        expected = self.store.project_event_projection_receipt_digest(
            event_key=str(row.get("event_key") or ""),
            event_type=str(row.get("event_type") or ""),
            event_digest=str(row.get("event_digest") or ""),
            occurred_at=str(row.get("occurred_at") or ""),
            journal_seq=row.get("journal_seq"),
            journal_event_id=str(row.get("journal_event_id") or ""),
            journal_recorded_at=str(row.get("journal_recorded_at") or ""),
        )
        if str(row.get("projection_receipt_digest") or "") != expected:
            raise ValueError("project outbox projection receipt digest mismatch")
        return {
            "expected_seq": row["journal_seq"],
            "expected_event_id": str(row["journal_event_id"]),
            "expected_recorded_at": str(row["journal_recorded_at"]),
            "expected_request_digest": event_record_request_digest(
                str(row["event_type"]),
                dict(payload),
                str(row["occurred_at"]),
            ),
        }

    def _acknowledge_row(self, row: Mapping[str, Any]) -> None:
        key = str(row.get("event_key") or "")
        payload = self._validated_payload(row)
        receipt = self._validated_projection_receipt(row, payload)
        acknowledged = self._acknowledge(key, **receipt)
        if not acknowledged:
            raise RuntimeError("journal event-key acknowledgement failed")
        self.store.acknowledge_project_event(key)

    def _cleanup_acknowledgements(self, *, limit: int) -> tuple[int, int]:
        cleaned = 0
        failed = 0
        for row in self.store.unacknowledged_project_events(limit=limit):
            key = str(row["event_key"])
            try:
                self._acknowledge_row(row)
                cleaned += 1
            except Exception as exc:
                self.store.fail_project_event(key, str(exc))
                failed += 1
        return cleaned, failed

    def run_once(self, *, limit: int = 100) -> Dict[str, Any]:
        bound = max(1, min(500, int(limit)))
        acknowledged, acknowledgement_failures = self._cleanup_acknowledgements(
            limit=bound,
        )
        projected = 0
        failed = 0
        retained = 0
        for row in self.store.pending_project_events(limit=bound):
            key = str(row["event_key"])
            try:
                payload = self._validated_payload(row)
                record = self._project(
                    str(row["event_type"]),
                    payload,
                    occurred_at=str(row["occurred_at"]),
                    event_key=key,
                )
                if not isinstance(record, Mapping):
                    raise RuntimeError("project event journal projection failed")
                completed = self.store.complete_project_event(key, dict(record))
                projected += 1
                retained += int(bool(record.get("retained", True)))
            except Exception as exc:
                self.store.fail_project_event(key, str(exc))
                failed += 1
                continue
            try:
                self._acknowledge_row(completed)
                acknowledged += 1
            except Exception as exc:
                self.store.fail_project_event(key, str(exc))
                acknowledgement_failures += 1
        status = self.store.project_event_outbox_status()
        return {
            "processed": projected + failed,
            "projected": projected,
            "failed": failed,
            "retained": retained,
            "acknowledged": acknowledged,
            "acknowledgement_failures": acknowledgement_failures,
            "outbox": status,
        }

    def status(self) -> Dict[str, Any]:
        return self.store.project_event_outbox_status()


__all__ = ["ProjectEventProjector"]
