"""Default handler registry for the PacoMind task queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from pacomind.task_queue.models import JobType
from pacomind.task_queue.worker import JobHandler
from pacomind.task_queue.handlers.monitoring import MonitoringHandler
from pacomind.task_queue.handlers.subtask_handler import SubtaskHandler
from pacomind.task_queue.handlers.system_maintenance import SystemMaintenanceHandler

if TYPE_CHECKING:
    from pacomind.router.router import LLMRouter
    from pacomind.world_model.store import WorldModelStore
    from pacomind.contacts.store import ContactStore
    from pacomind.task_queue.handlers.inference import _InferenceGateSessionStore


def build_default_handlers(
    router: Optional["LLMRouter"] = None,
    world_model_store: Optional["WorldModelStore"] = None,
    contact_store: Optional["ContactStore"] = None,
    response_gate: Optional[Any] = None,
    gate_session_store: Optional["_InferenceGateSessionStore"] = None,
    desktop_config: Optional[Any] = None,
    browser_config: Optional[Any] = None,
    node_id: str = "",
) -> Dict[JobType, JobHandler]:
    """Assemble the default set of job handlers.

    Args:
        router: LLMRouter instance. Required for INFERENCE jobs.
                If None, InferenceHandler is omitted from the registry.
        world_model_store: Optional WorldModelStore for context enrichment.
        contact_store: Optional ContactStore for contact resolution.
        response_gate: Optional ResponseGate for outbound response filtering.
        gate_session_store: Session store adapter used by the ResponseGate.
        desktop_config: Retired unsupported parameter; must be None.
        browser_config: Retired unsupported parameter; must be None.
        node_id: Legacy compatibility parameter, unused by supported handlers.

    Returns:
        Dict mapping JobType → JobHandler instance.
    """
    if desktop_config is not None or browser_config is not None:
        raise ValueError(
            "Desktop/browser queue workers are unsupported. Remove desktop_config "
            "and browser_config; use the native runtime's registered tools."
        )
    handlers: Dict[JobType, JobHandler] = {
        JobType.MONITORING: MonitoringHandler(),
        JobType.SYSTEM_MAINTENANCE: SystemMaintenanceHandler(),
        JobType.CUSTOM: SubtaskHandler(),
    }

    if router is not None:
        from pacomind.task_queue.handlers.inference import (
            InferenceHandler,
            ThoughtOnlyInferenceHandler,
        )
        inference_handler = InferenceHandler(
            router,
            world_model_store=world_model_store,
            contact_store=contact_store,
            response_gate=response_gate,
            gate_session_store=gate_session_store,
        )
        handlers[JobType.INFERENCE] = inference_handler
        # ThoughtJobV1 shares only the router transport. A distinct strict
        # handler makes it impossible for a registry mix-up to run a generic
        # inference payload on the owner-private cognition lane.
        handlers[JobType.THOUGHT] = ThoughtOnlyInferenceHandler(router)

    return handlers
