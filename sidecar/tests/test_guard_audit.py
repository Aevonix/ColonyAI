"""GuardAuditStore: records cross-context guard events split by authorized vs not, so the
classifier can be measured in shadow before enforcing."""
import pytest
from colony_sidecar.gate.guard_audit import GuardAuditStore


def test_records_and_summarizes():
    a = GuardAuditStore(":memory:")
    a.record(conversation_key="rcs:B", mode="shadow", decision="allow", authorized=False,
             checks=["cross_context"], entities=["[falcon]"], response_text="re Falcon")
    a.record(conversation_key="rcs:C", mode="shadow", decision="allow", authorized=True,
             checks=["cross_context"], entities=["[falcon]"], response_text="sharing as asked")
    s = a.summary()
    assert s == {"total": 2, "authorized_transfers": 1, "unauthorized_flags": 1}
    assert len(a.recent(authorized=True)) == 1
    assert a.recent(authorized=True)[0]["conversation_key"] == "rcs:C"
