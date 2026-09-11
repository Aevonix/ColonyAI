"""Thought handler contracts against the real selector and local HTTP transport."""

import asyncio
from datetime import datetime, timezone
import json

import pytest

from apsimo.cognition.goal_spine import ThoughtJobV1, parse_thought_output
from apsimo.self_model.workspace import ConcernStore
from apsimo.task_queue.handlers.inference import InferenceHandler
from apsimo.task_queue.models import Job, JobType
from test_cognition_goal_spine import concern
from test_function_routing import config, endpoint, router
from test_inference_context_gate import _big_doc


@pytest.mark.asyncio
@pytest.mark.parametrize("task_override", [False, True])
async def test_strict_thought_uses_selected_function_and_its_deadline(
    tmp_path, monkeypatch, task_override,
):
    thought = ThoughtJobV1.for_concern(
        concern(ConcernStore(str(tmp_path / "concerns.db"))),
        attempt_number=1,
        allowed_read_capabilities=("memory:read", "reasoning"),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
        max_output_tokens=128,
    )
    answer = json.dumps({
        "kind": "Note", "evidence_refs": list(thought.source_refs),
        "confidence": 0.8, "note": "The supplied concern remains open.",
    })
    timeouts = []
    original_timeout = asyncio.timeout

    def record_timeout(delay):
        timeouts.append(delay)
        return original_timeout(delay)

    monkeypatch.setattr(asyncio, "timeout", record_timeout)
    with endpoint(content=answer) as (url, requests):
        cfg = config(url, url)
        cfg["functionRoles"]["reasoning"] = {
            "candidates": ["deliberate"], "timeoutSeconds": 7, "deadlineSeconds": 11,
        }
        cfg["functionRoles"]["planning"] = {
            "candidates": ["interactive"], "timeoutSeconds": 13, "deadlineSeconds": 17,
        }
        if task_override:
            cfg["taskRoles"] = {"thought_job": "planning"}
        selected = router(cfg)
        result = await InferenceHandler(selected).execute(Job(
            job_id=thought.thought_job_id, job_type=JobType.THOUGHT,
            payload=thought.payload(),
        ))
        assert len(requests) == 1
        body = requests[0]["payload"]
        assert body["model"] == ("fast-neutral" if task_override else "strong-neutral")
        assert body["messages"] == [
            {"role": "system", "content": thought.system_prompt},
            {"role": "user", "content": thought.prompt},
        ]
        assert body["max_tokens"] == 128
        assert timeouts[0] == (13 if task_override else 7)
        assert selected.function_deadline_seconds(context={"task": "thought_job"}) == (
            17 if task_override else 11
        )
        bound = parse_thought_output(result["thought_output"], thought)
        assert bound.thought_job_id == thought.thought_job_id
        assert bound.payload["note"] == "The supplied concern remains open."


@pytest.mark.asyncio
@pytest.mark.parametrize("thought_budget,default_budget", [(4000, 128000), (128000, 4000)])
async def test_generic_cognition_gate_uses_the_same_task_role_as_dispatch(
    monkeypatch, thought_budget, default_budget,
):
    monkeypatch.setenv("COLONY_CONTEXT_GATE", "auto")
    doc = _big_doc() + "\n\nWhen did the database outage start?"
    with endpoint(content="The recorded outage began at 03:14 UTC.") as (url, requests):
        cfg = config(url, url)
        cfg["modelPool"]["interactive"]["contextTokens"] = default_budget
        cfg["modelPool"]["deliberate"]["contextTokens"] = thought_budget
        for binding in cfg["modelPool"].values():
            binding["maxTokens"] = 128
        cfg["functionRoles"]["reasoning"] = ["interactive"]
        cfg["functionRoles"]["planning"] = ["deliberate"]
        cfg["taskRoles"] = {"thought_job": "planning"}
        selected = router(cfg)
        result = await InferenceHandler(selected).execute(Job(
            job_type=JobType.INFERENCE,
            payload={"prompt": doc, "cognition_read_only": True, "max_output_tokens": 64},
        ))
        assert result["status"] == "completed"
        assert len(requests) == 1
        body = requests[0]["payload"]
        assert body["model"] == "strong-neutral"
        assert body["max_tokens"] == 64
        received = body["messages"][-1]["content"]
        assert "03:14" in received
        if thought_budget < default_budget:
            assert len(received) < len(doc)
        else:
            assert received == doc
