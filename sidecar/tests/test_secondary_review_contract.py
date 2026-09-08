"""Optional review status never turns unavailable/invalid into a valid verdict."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from colony_sidecar.gate.config import GateConfig
from colony_sidecar.gate.layers.l6_review import SecondaryReviewer
from colony_sidecar.gate.models import GatePayload
from colony_sidecar.gate.pipeline import ResponseGate
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier


def payload():
    return GatePayload("The fixture is ready.", "fixture-contact", "sms", "",
                       TrustTier.REGULAR, frozenset(), "fixture-turn", "Status?")


@pytest.mark.parametrize("output,flagged,status,category", [
    ('{"verdict":"appropriate"}', False, "reviewed", None),
    ('{"verdict":"flag_for_review","category":"fixture"}', True, "reviewed", "fixture"),
    ('{"verdict":"unknown"}', True, "invalid", "review_invalid"),
    ('{}', True, "invalid", "review_invalid"),
    ('[]', True, "invalid", "review_invalid"),
    ('{"verdict":[]}', True, "invalid", "review_invalid"),
    ('{"verdict":"flag_for_review","category":[]}', True, "invalid", "review_invalid"),
    ('not JSON', True, "invalid", "review_invalid"),
])
async def test_verdicts_are_classified_once(output, flagged, status, category):
    client = SimpleNamespace(complete=AsyncMock(return_value=output))
    result = await SecondaryReviewer(llm_client=client).review(payload())
    assert (result.flagged, result.status, result.category) == (flagged, status, category)
    assert client.complete.await_count == 1


@pytest.mark.parametrize("error", [RuntimeError, ValueError, TypeError])
async def test_missing_and_failed_client_are_unavailable(error):
    missing = await SecondaryReviewer().review(payload())
    assert (missing.flagged, missing.status, missing.category) == (True, "unavailable", "review_unavailable")
    client = SimpleNamespace(complete=AsyncMock(side_effect=error("fixture unavailable")))
    failed = await SecondaryReviewer(llm_client=client).review(payload())
    assert (failed.flagged, failed.status, failed.category) == (True, "unavailable", "review_error")
    assert client.complete.await_count == 1


async def test_default_pipeline_skips_optional_review_but_enabled_pipeline_records_unavailability():
    audit = SimpleNamespace(record=AsyncMock())
    sessions = SimpleNamespace(get_recent_other_sessions=AsyncMock(return_value={}))
    default = await ResponseGate(GateConfig(), sessions, audit).evaluate(payload())
    assert not default.blocked
    assert default.layer_results["layer_6"] == {"skipped": True}
    enabled = await ResponseGate(GateConfig(enable_secondary_review=True), sessions, audit).evaluate(payload())
    assert enabled.blocked and enabled.blocking_layer == 6
    assert enabled.block_reason == "secondary_review_unavailable"
    assert enabled.layer_results["layer_6"] == {
        "flagged": True, "category": "review_unavailable", "status": "unavailable"}
