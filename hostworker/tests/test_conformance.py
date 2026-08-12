"""The conformance suite passes the reference store AND catches broken ones.

The broken stores below each violate exactly one documented invariant the
way a plausible buggy adapter would; the suite must flag every one.  If a
new invariant is added to :mod:`colony_hostworker.store`, add both a case
and a broken store here.
"""

import pytest

from colony_hostworker.conformance import (
    CASES,
    SqliteStoreHarness,
    assert_store_conformance,
    run_store_conformance,
    sqlite_harness,
)
from colony_hostworker.gate import GateAuthorization
from colony_hostworker.sqlite_store import SqliteActionStore


def failed_names(results):
    return {result.name for result in results if not result.passed}


def test_reference_store_passes_every_case():
    results = assert_store_conformance(sqlite_harness)
    assert len(results) == len(CASES)
    assert all(result.passed for result in results)


def broken_factory(store_class):
    def factory():
        return SqliteStoreHarness(store_class=store_class)

    return factory


class ValidatorSkippingStore(SqliteActionStore):
    """I4 violation: 'implements the signature' but never runs the caller's
    validator — substitutes its own naive structural extraction instead."""

    def begin_owner_authorized_dispatch(self, action_id, owner, *, gate_validator, **kwargs):
        def naive(action, receipts, now):
            gate = next(
                receipt for receipt in receipts if receipt["kind"] == "gate"
            )
            evidence = gate["evidence"]
            return GateAuthorization(
                shape="naive",
                granted=False,
                receipt_key=gate["receipt_key"],
                evidence_sha256=gate["evidence_sha256"],
                approval_id=evidence["approval_id"],
                decision_id=evidence["decision_id"],
                revision=1,
                decided_at=float(evidence["decided_at_epoch"]),
                expires_at=float(evidence["expires_at_epoch"]),
                expired=False,
            )

        return super().begin_owner_authorized_dispatch(
            action_id, owner, gate_validator=naive, **kwargs
        )


def test_suite_catches_a_store_that_skips_the_gate_validator():
    results = run_store_conformance(broken_factory(ValidatorSkippingStore))
    failures = failed_names(results)
    assert "check_gate_validator_runs_inside_dispatch" in failures
    # Only the shape-aware validator knows the grant expiry field, so a
    # validator-skipping store dispatches on a dead grant:
    assert "check_expired_grant_at_point_of_use" in failures


class RegatingStore(SqliteActionStore):
    """I6/I10 violation: the classic general-purpose retry edge — a
    'retryable' dispatched failure goes back to gated for another attempt."""

    def fail_attempt(self, action_id, owner, error, retryable):
        if retryable:
            now = float(self._clock())
            with self._transaction() as cursor:
                row = self._get_action_row(cursor, action_id)
                self._require_lease(row, owner, now)
                if row["state"] == "dispatched":
                    cursor.execute(
                        """UPDATE actions SET state='gated', last_error=?,
                           lease_owner=NULL, lease_expires_at=NULL,
                           updated_at=?, next_attempt_at=? WHERE action_id=?""",
                        (str(error), now, now, action_id),
                    )
                    self._add_event(
                        cursor,
                        action_id,
                        "attempt_failed_retrying",
                        "dispatched",
                        "gated",
                        owner,
                        {"error": str(error)},
                        now,
                    )
                    return self._action_dict(
                        self._get_action_row(cursor, action_id)
                    )
        return super().fail_attempt(action_id, owner, error, retryable)


def test_suite_catches_a_store_that_regates_dispatched_work():
    results = run_store_conformance(broken_factory(RegatingStore))
    assert "check_dispatched_never_regates" in failed_names(results)


class LeaselessStore(SqliteActionStore):
    """I3 violation: lease ownership is never actually checked."""

    @staticmethod
    def _require_lease(row, owner, now):
        return None


def test_suite_catches_a_store_without_lease_enforcement():
    results = run_store_conformance(broken_factory(LeaselessStore))
    assert "check_lease_steal_during_observation" in failed_names(results)


class SecondGateTolerantStore(SqliteActionStore):
    """Adversarial-duplicate violation: picks 'the matching' gate receipt
    instead of requiring exactly one."""

    def begin_owner_authorized_dispatch(self, action_id, owner, **kwargs):
        gate_receipt_key = kwargs.get("gate_receipt_key")
        with self._lock:
            cursor = self._conn.cursor()
            try:
                rows = cursor.execute(
                    "SELECT receipt_id FROM receipts "
                    "WHERE action_id=? AND kind='gate' AND receipt_key!=?",
                    (action_id, gate_receipt_key),
                ).fetchall()
                # Hide every non-matching gate from the parent implementation
                # by deleting it (dropping the immutability trigger first, as
                # a buggy adapter with its own schema effectively does).
                if rows:
                    cursor.execute("DROP TRIGGER IF EXISTS receipts_no_delete")
                    cursor.execute(
                        "DELETE FROM receipts "
                        "WHERE action_id=? AND kind='gate' AND receipt_key!=?",
                        (action_id, gate_receipt_key),
                    )
                    self._conn.commit()
            finally:
                cursor.close()
        return super().begin_owner_authorized_dispatch(action_id, owner, **kwargs)


def test_suite_catches_a_store_that_tolerates_duplicate_gates():
    results = run_store_conformance(broken_factory(SecondGateTolerantStore))
    failures = failed_names(results)
    assert "check_duplicate_gates_refused" in failures
    assert "check_evidence_mutated_between_precheck_and_dispatch" in failures


def test_module_runner_reports_reference_success(capsys):
    from colony_hostworker.conformance.__main__ import main

    assert main() == 0
    output = capsys.readouterr().out
    assert "%d/%d conformance cases passed" % (len(CASES), len(CASES)) in output


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.__name__)
def test_each_case_names_what_it_catches(case):
    """Every case documents its invariant or failure mode in its docstring,
    so a failing run tells the adapter author what is at stake."""

    docstring = case.__doc__ or ""
    assert len(docstring.strip()) > 40
