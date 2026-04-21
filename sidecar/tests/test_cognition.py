"""Tests for cognition substrate — Layer 3: Prompt + Trigger."""

import os
import pytest
from unittest.mock import patch

from colony_sidecar.cognition.prompt import build_cognition_prompt, COGNITION_SYSTEM_PROMPT
from colony_sidecar.cognition.trigger import trigger_cognition, _cognition_enabled, _cognition_model


class TestCognitionPrompt:
    def test_system_prompt_exists(self):
        assert len(COGNITION_SYSTEM_PROMPT) > 100
        assert "COMMITMENTS" in COGNITION_SYSTEM_PROMPT

    def test_turn_sync_prompt(self):
        prompt = build_cognition_prompt(
            trigger_type="turn_sync",
            context={
                "conversation_text": "I'll check on the cluster by Friday",
                "person_id": "marc",
            },
        )
        assert "turn_sync" in prompt
        assert "marc" in prompt
        assert "cluster" in prompt

    def test_turn_sync_with_existing_commitments(self):
        existing = [
            {"description": "Review PR", "due_at": "2026-04-25"},
            {"description": "Call Sarah", "due_at": None},
        ]
        prompt = build_cognition_prompt(
            trigger_type="turn_sync",
            context={"conversation_text": "Test", "person_id": "marc"},
            existing_commitments=existing,
        )
        assert "Existing pending" in prompt
        assert "Review PR" in prompt
        assert "Call Sarah" in prompt

    def test_signal_ingest_prompt(self):
        prompt = build_cognition_prompt(
            trigger_type="signal_ingest",
            context={"signal_type": "engagement", "signal_data": {"score": 0.8}},
        )
        assert "signal_ingest" in prompt
        assert "engagement" in prompt

    def test_anomaly_prompt(self):
        prompt = build_cognition_prompt(
            trigger_type="anomaly",
            context={"description": "Unusual login pattern detected"},
        )
        assert "anomaly" in prompt
        assert "Unusual" in prompt

    def test_manual_prompt(self):
        prompt = build_cognition_prompt(
            trigger_type="manual",
            context={"prompt": "Review all pending commitments"},
        )
        assert "manual" in prompt
        assert "Review all" in prompt


class TestCognitionTrigger:
    @pytest.mark.asyncio
    async def test_disabled_returns_not_accepted(self):
        with patch.dict(os.environ, {"COLONY_COGNITION_ENABLED": "false"}):
            result = await trigger_cognition("turn_sync", {"conversation_text": "test"})
            assert result["accepted"] is False
            assert "disabled" in result["message"]

    @pytest.mark.asyncio
    async def test_no_model_returns_not_accepted(self):
        with patch.dict(os.environ, {"COLONY_COGNITION_ENABLED": "true", "COLONY_COGNITION_MODEL": ""}):
            result = await trigger_cognition("turn_sync", {"conversation_text": "test"})
            assert result["accepted"] is False
            assert "MODEL" in result["message"]

    @pytest.mark.asyncio
    async def test_enabled_with_model_accepts(self):
        with patch.dict(os.environ, {
            "COLONY_COGNITION_ENABLED": "true",
            "COLONY_COGNITION_MODEL": "gemma-4-31b",
            "COLONY_COGNITION_THROTTLE_SECONDS": "0",
        }):
            result = await trigger_cognition("turn_sync", {"conversation_text": "I'll check tomorrow"})
            assert result["accepted"] is True

    @pytest.mark.asyncio
    async def test_high_priority_bypasses_throttle(self):
        import colony_sidecar.cognition.trigger as trig
        # Set a recent trigger time
        trig._last_trigger_time = 9999999999.0

        with patch.dict(os.environ, {
            "COLONY_COGNITION_ENABLED": "true",
            "COLONY_COGNITION_MODEL": "gemma-4-31b",
            "COLONY_COGNITION_THROTTLE_SECONDS": "300",
        }):
            result = await trigger_cognition(
                "turn_sync",
                {"conversation_text": "urgent"},
                priority="high",
            )
            assert result["accepted"] is True

    @pytest.mark.asyncio
    async def test_normal_priority_throttled(self):
        import colony_sidecar.cognition.trigger as trig
        trig._last_trigger_time = 9999999999.0

        with patch.dict(os.environ, {
            "COLONY_COGNITION_ENABLED": "true",
            "COLONY_COGNITION_MODEL": "gemma-4-31b",
            "COLONY_COGNITION_THROTTLE_SECONDS": "300",
        }):
            result = await trigger_cognition(
                "turn_sync",
                {"conversation_text": "normal"},
                priority="normal",
            )
            assert result["accepted"] is True
            assert result["throttle_seconds"] is not None


class TestCognitionConfig:
    def test_enabled_default_false(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove the key entirely
            os.environ.pop("COLONY_COGNITION_ENABLED", None)
            assert _cognition_enabled() is False

    def test_enabled_true(self):
        with patch.dict(os.environ, {"COLONY_COGNITION_ENABLED": "true"}):
            assert _cognition_enabled() is True

    def test_model_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("COLONY_COGNITION_MODEL", None)
            assert _cognition_model() is None

    def test_model_set(self):
        with patch.dict(os.environ, {"COLONY_COGNITION_MODEL": "gpt-4o-mini"}):
            assert _cognition_model() == "gpt-4o-mini"
