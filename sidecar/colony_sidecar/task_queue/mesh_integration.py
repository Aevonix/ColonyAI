"""Mesh layer integration for the Colony distributed task queue.

Bridges SWIM node-dead events and mesh role-change events into
queue scheduler actions. Wires the task queue into the existing
colony/mesh/ layer without modifying it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from colony_sidecar.task_queue.queue_manager import QueueManager
from colony_sidecar.task_queue.scheduler import Scheduler

logger = logging.getLogger(__name__)


class QueueMeshEventHandler:
    """Handles mesh-layer events that affect the task queue.

    Subscribe this handler to the Colony event bus for MeshEvent types.

    Responsibilities:
    - On NODE_DEAD: immediately abandon all jobs claimed by the dead node.
    - On ROLE_CHANGE to SOVEREIGN (self): assume the scheduler role.
    """

    def __init__(
        self,
        queue: QueueManager,
        scheduler: Scheduler,
        own_node_id: str,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._queue = queue
        self._scheduler = scheduler
        self._own_node_id = own_node_id
        self._event_bus = event_bus

    async def on_node_dead(self, node_id: str) -> None:
        """Immediately abandon all jobs claimed by the dead node.

        Called when SWIM emits a NODE_DEAD event. Does not wait for the
        heartbeat timeout — fast-path abandonment.
        """
        abandoned = await self._queue.abandon_jobs_for_node(node_id)
        if abandoned:
            logger.info(
                "QueueMeshEventHandler: abandoned %d jobs from dead node %s: %s",
                len(abandoned), node_id, abandoned,
            )
        # Trigger immediate redistribution
        await self._queue.requeue_retryable_jobs(
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        )

    async def on_role_change(self, new_role: str, node_id: str) -> None:
        """Handle mesh role-change events.

        If this node becomes SOVEREIGN (Queen), start the scheduler loop.
        """
        if new_role == "sovereign" and node_id == self._own_node_id:
            logger.info(
                "QueueMeshEventHandler: this node (%s) became Sovereign — "
                "starting scheduler", self._own_node_id,
            )
            import asyncio
            asyncio.create_task(self._scheduler.run())

    def handle_mesh_event(self, event: Any) -> None:
        """Synchronous event bus callback. Dispatches to async handlers."""
        import asyncio
        node_id = getattr(event, "node_id", "")
        event_type = getattr(event, "event_type", "")

        if event_type == "node_dead" or event_type == "NODE_DEAD":
            asyncio.create_task(self.on_node_dead(node_id))
        elif event_type in {"role_changed", "ROLE_CHANGE"}:
            new_role = ""
            new_role_attr = getattr(event, "new_role", None)
            if new_role_attr is not None:
                new_role = str(new_role_attr.value if hasattr(new_role_attr, "value") else new_role_attr)
            asyncio.create_task(self.on_role_change(new_role, node_id))

    def subscribe_to_event_bus(self) -> None:
        """Register this handler on the Colony event bus if one is configured."""
        if self._event_bus is None:
            return
        from colony_sidecar.events.types import MeshEvent
        self._event_bus.subscribe(
            handler=self.handle_mesh_event,
            event_types=[MeshEvent],
        )
        logger.info(
            "QueueMeshEventHandler: subscribed to MeshEvent on event bus"
        )
