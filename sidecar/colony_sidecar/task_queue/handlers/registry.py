"""Default handler registry for the Colony task queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from colony_sidecar.task_queue.models import JobType
from colony_sidecar.task_queue.worker import JobHandler
from colony_sidecar.task_queue.handlers.monitoring import MonitoringHandler
from colony_sidecar.task_queue.handlers.subtask_handler import SubtaskHandler
from colony_sidecar.task_queue.handlers.system_maintenance import SystemMaintenanceHandler

if TYPE_CHECKING:
    from colony_sidecar.router.llm_router import LLMRouter
    from colony_sidecar.world_model.store import WorldModelStore
    from colony_sidecar.contacts.store import ContactStore
    from colony_sidecar.task_queue.handlers.inference import _InferenceGateSessionStore
    # desktop/browser worker packages do not exist yet (docs/KNOWN-GAPS.md);
    # their config params stay Any so type checkers don't chase ghost modules.


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
        desktop_config: Optional DesktopConfig. If provided and enabled,
                        registers DesktopJobHandler for DESKTOP jobs.
        browser_config: Optional BrowserConfig. If provided and enabled,
                        registers BrowserJobHandler for BROWSER jobs.
        node_id: Worker node identifier, passed to desktop/browser handlers.

    Returns:
        Dict mapping JobType → JobHandler instance.
    """
    handlers: Dict[JobType, JobHandler] = {
        JobType.MONITORING: MonitoringHandler(),
        JobType.SYSTEM_MAINTENANCE: SystemMaintenanceHandler(),
        JobType.CUSTOM: SubtaskHandler(),
    }

    if router is not None:
        from colony_sidecar.task_queue.handlers.inference import InferenceHandler
        handlers[JobType.INFERENCE] = InferenceHandler(
            router,
            world_model_store=world_model_store,
            contact_store=contact_store,
            response_gate=response_gate,
            gate_session_store=gate_session_store,
        )

    if desktop_config is not None and desktop_config.enabled:
        try:
            from colony_sidecar.desktop.worker import DesktopJobHandler
            desktop_handler = DesktopJobHandler(desktop_config, node_id=node_id)
            if response_gate is not None:
                desktop_handler.set_gate(response_gate)
            handlers[JobType.DESKTOP] = desktop_handler
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "DesktopJobHandler init failed — desktop jobs will not execute: %s", exc
            )

    if browser_config is not None and browser_config.enabled:
        try:
            from colony_sidecar.browser.worker import BrowserJobHandler
            browser_handler = BrowserJobHandler(browser_config, node_id=node_id)
            if response_gate is not None:
                browser_handler.set_gate(response_gate)
            handlers[JobType.BROWSER] = browser_handler
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "BrowserJobHandler init failed — browser jobs will not execute: %s", exc
            )

    return handlers
