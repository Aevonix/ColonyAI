"""Custom subtask handler for the Colony task queue.

Handles job_type=CUSTOM jobs dispatched by GoalQueueBridge.
For simple subtasks, spawns a lightweight Sonnet agent to execute the task
and returns the result as a job output dict.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from colony_sidecar.task_queue.models import Job
from colony_sidecar.task_queue.worker import JobHandler

logger = logging.getLogger(__name__)

_SONNET_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024

_SYSTEM_PROMPT = (
    "You are a Colony AI subtask executor. Complete the given task concisely. "
    "Respond with a brief completion report summarising what was done. "
    "Do not ask clarifying questions."
)


class SubtaskHandler(JobHandler):
    """Executes custom subtasks dispatched by GoalQueueBridge.

    The job payload is expected to contain:
        goal_id     — parent goal identifier
        subtask_id  — subtask identifier
        description — natural-language task description (preferred)
        title / task / action / objective — fallback description keys

    For each subtask the handler calls the Anthropic API with the Sonnet
    model (restricted to text-only, no tools) and returns the model's
    completion report as the job output.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    async def execute(self, job: Job) -> Dict[str, Any]:
        goal_id = job.payload.get("goal_id", "")
        subtask_id = job.payload.get("subtask_id", "")
        task_description = job.payload.get("description", "") or _build_description(job.payload)

        logger.info(
            "SubtaskHandler: executing subtask %s for goal %s",
            subtask_id,
            goal_id,
        )

        result = await self._run_agent(task_description, goal_id, subtask_id)

        logger.info(
            "SubtaskHandler: subtask %s finished (goal=%s, success=%s)",
            subtask_id,
            goal_id,
            result.get("success"),
        )
        return result

    async def _run_agent(
        self,
        task_description: str,
        goal_id: str,
        subtask_id: str,
    ) -> Dict[str, Any]:
        """Call a lightweight Sonnet agent to execute the task."""
        if not self._api_key:
            logger.warning(
                "SubtaskHandler: ANTHROPIC_API_KEY not set — subtask %s skipped",
                subtask_id,
            )
            return {
                "success": False,
                "error": "ANTHROPIC_API_KEY not configured",
                "goal_id": goal_id,
                "subtask_id": subtask_id,
            }

        try:
            import anthropic
        except ImportError:
            logger.error("SubtaskHandler: anthropic package not installed")
            return {
                "success": False,
                "error": "anthropic package not available",
                "goal_id": goal_id,
                "subtask_id": subtask_id,
            }

        # Try ModelRegistry utility role for model/credentials; fall back to hardcoded
        _model = _SONNET_MODEL
        _api_key = self._api_key
        try:
            from colony_cli.model_config import load_model_registry
            _registry = load_model_registry()
            _rt = _registry.resolve_runtime(role="utility")
            if _rt.get("model"):
                _model = _rt["model"]
            if _rt.get("api_key"):
                _api_key = _rt["api_key"]
        except Exception:
            pass

        client = anthropic.AsyncAnthropic(api_key=_api_key)
        try:
            message = await client.messages.create(
                model=_model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Goal ID: {goal_id}\n"
                            f"Subtask ID: {subtask_id}\n"
                            f"Task: {task_description}"
                        ),
                    }
                ],
            )
            output_text = message.content[0].text if message.content else ""
            return {
                "success": True,
                "output": output_text,
                "goal_id": goal_id,
                "subtask_id": subtask_id,
                "model": _model,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            logger.error(
                "SubtaskHandler: agent call failed for subtask %s: %s",
                subtask_id,
                exc,
            )
            return {
                "success": False,
                "error": str(exc),
                "goal_id": goal_id,
                "subtask_id": subtask_id,
            }


def _build_description(payload: Dict[str, Any]) -> str:
    """Construct a task description from payload when 'description' is absent."""
    for key in ("title", "task", "action", "objective"):
        if payload.get(key):
            return str(payload[key])
    skip = {"goal_id", "subtask_id"}
    parts = [f"{k}: {v}" for k, v in payload.items() if k not in skip and v]
    return " — ".join(parts) or "Execute subtask"
