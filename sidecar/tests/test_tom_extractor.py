"""Tests for ToM LLM Extractor."""

import pytest
import json

from colony_sidecar.tom.extractor import (
    TomExtractor,
    _parse_affect_json,
    _parse_fact_array,
)


class FakeResponse:
    def __init__(self, content: str):
        self.content = content


class FakeRouter:
    """Fake LLM router for testing."""

    def __init__(self, responses=None):
        self.responses = responses or []
        self._idx = 0

    async def complete(self, messages, context=None, **kwargs):
        if self._idx < len(self.responses):
            resp = self.responses[self._idx]
            self._idx += 1
            return FakeResponse(resp)
        return FakeResponse("")


class TestParseAffectJson:
    def test_valid_json(self):
        raw = '{"valence": 0.5, "arousal": 0.8, "trigger": "good news", "confidence": 0.9}'
        result = _parse_affect_json(raw)
        assert result is not None
        assert result["valence"] == 0.5
        assert result["arousal"] == 0.8
        assert result["trigger"] == "good news"

    def test_with_code_fence(self):
        raw = '```json\n{"valence": -0.3, "arousal": 0.6, "trigger": "frustration", "confidence": 0.7}\n```'
        result = _parse_affect_json(raw)
        assert result is not None
        assert result["valence"] == -0.3

    def test_neutral_skipped(self):
        raw = '{"valence": 0.0, "arousal": 0.3, "trigger": null, "confidence": 0.5}'
        result = _parse_affect_json(raw)
        assert result is None  # Neutral reading not worth storing

    def test_valence_clamped(self):
        raw = '{"valence": 2.0, "arousal": 0.5, "confidence": 0.8}'
        result = _parse_affect_json(raw)
        assert result is not None
        assert result["valence"] == 1.0

    def test_invalid_json(self):
        result = _parse_affect_json("not json")
        assert result is None

    def test_empty(self):
        result = _parse_affect_json("")
        assert result is None

    def test_json_embedded_in_text(self):
        raw = 'Here is the analysis: {"valence": 0.4, "arousal": 0.7, "trigger": "excited", "confidence": 0.8}'
        result = _parse_affect_json(raw)
        assert result is not None
        assert result["valence"] == 0.4


def fact_item(evidence="test fact", **kwargs):
    return {"evidence": evidence, "source": "told_by_contact", "confidence": 0.8,
            "memory_kind": "personal_context", "recall_reason": "Recall relevant context when assisting this person.", **kwargs}


class TestParseFactArray:
    def test_valid_array(self):
        raw = json.dumps([fact_item("I prefer tea", source="told_by_contact")])
        result = _parse_fact_array(raw, conversation_text="user: I prefer tea")
        assert len(result) == 1
        assert result[0]["fact"] == "I prefer tea"
        assert result[0]["source"] == "told_by_contact"

    def test_with_code_fence(self):
        raw = '```json\n' + json.dumps([fact_item()]) + '\n```'
        result = _parse_fact_array(raw, conversation_text="test fact")
        assert len(result) == 1

    def test_empty_array(self):
        result = _parse_fact_array("[]")
        assert result == []

    def test_invalid_source_default(self):
        raw = json.dumps([fact_item(source="unknown")])
        result = _parse_fact_array(raw, conversation_text="test fact")
        assert len(result) == 1
        assert result[0]["source"] == "inferred"

    def test_missing_fact_skipped(self):
        raw = '[{"source": "inferred", "confidence": 0.5}]'
        result = _parse_fact_array(raw)
        assert len(result) == 0

    def test_dict_with_facts_key(self):
        raw = json.dumps({"facts": [fact_item()]})
        result = _parse_fact_array(raw, conversation_text="test fact")
        assert len(result) == 1

    def test_confidence_clamped(self):
        raw = json.dumps([fact_item(confidence=2.0)])
        result = _parse_fact_array(raw, conversation_text="test fact")
        assert result[0]["confidence"] == 1.0


@pytest.mark.asyncio
class TestTomExtractor:
    async def test_extract_affect(self):
        router = FakeRouter([
            '{"valence": 0.6, "arousal": 0.7, "trigger": "release success", "confidence": 0.85}'
        ])
        extractor = TomExtractor(router)
        result = await extractor.extract_affect("Great, v0.5.0 is live!", "owner")
        assert result is not None
        assert result["valence"] == 0.6
        assert result["source"] == "inferred"
        assert result["contact_id"] == "owner"

    async def test_extract_affect_neutral(self):
        router = FakeRouter([
            '{"valence": 0.0, "arousal": 0.3, "trigger": null, "confidence": 0.5}'
        ])
        extractor = TomExtractor(router)
        result = await extractor.extract_affect("ok sure", "owner")
        assert result is None  # Neutral skipped

    async def test_extract_affect_empty(self):
        router = FakeRouter([])
        extractor = TomExtractor(router)
        result = await extractor.extract_affect("", "owner")
        assert result is None

    async def test_extract_facts(self):
        router = FakeRouter([
            json.dumps([fact_item("I prefer tea", memory_kind="preference")])
        ])
        extractor = TomExtractor(router)
        result = await extractor.extract_facts("user: I prefer tea", "owner")
        assert len(result) == 1
        assert result[0]["fact"] == "I prefer tea"
        assert result[0]["contact_id"] == "owner"

    async def test_extract_facts_empty(self):
        router = FakeRouter(["[]"])
        extractor = TomExtractor(router)
        result = await extractor.extract_facts("ok", "owner")
        assert result == []

    async def test_throttle_per_contact(self):
        router = FakeRouter([
            '{"valence": 0.5, "arousal": 0.7, "trigger": "test", "confidence": 0.8}',
            '{"valence": 0.3, "arousal": 0.4, "trigger": "test2", "confidence": 0.7}',
        ])
        extractor = TomExtractor(router)
        # First extraction should work
        result1 = await extractor.extract_affect("happy", "owner")
        assert result1 is not None
        # Second extraction for same contact should be throttled
        result2 = await extractor.extract_affect("still happy", "owner")
        assert result2 is None
        # Different contact should work
        result3 = await extractor.extract_affect("happy", "alice")
        assert result3 is not None

    async def test_llm_failure_returns_none(self):
        class FailRouter:
            async def complete(self, **kwargs):
                raise RuntimeError("LLM down")
        extractor = TomExtractor(FailRouter())
        result = await extractor.extract_affect("test", "owner")
        assert result is None
        facts = await extractor.extract_facts("test", "owner")
        assert facts == []

    async def test_affect_and_facts_run_same_contact_same_clock(self, monkeypatch):
        from datetime import datetime, timezone
        import colony_sidecar.tom.extractor as module

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(module, "datetime", FixedClock)
        monkeypatch.setattr(module, "THROTTLE_MINUTES", 5)
        router = FakeRouter([
            '{"valence": 0.6, "arousal": 0.7, "trigger": "boat trip", "confidence": 0.9}',
            json.dumps([fact_item("the hydrofoil leaves Friday", memory_kind="substantive_event")]),
        ])
        extractor = TomExtractor(router)
        text = "I am excited that the hydrofoil leaves Friday."
        assert await extractor.extract_affect(text, "same-person") is not None
        assert len(await extractor.extract_facts(text, "same-person")) == 1
        assert router._idx == 2
        # Each operation still throttles its own repeats, without another model call.
        assert await extractor.extract_affect(text, "same-person") is None
        assert await extractor.extract_facts(text, "same-person") == []
        assert router._idx == 2
