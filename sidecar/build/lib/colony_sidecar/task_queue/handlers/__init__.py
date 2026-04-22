"""Concrete JobHandler subclasses for Colony's distributed task queue."""

from colony_sidecar.task_queue.handlers.inference import InferenceHandler
from colony_sidecar.task_queue.handlers.monitoring import MonitoringHandler
from colony_sidecar.task_queue.handlers.system_maintenance import SystemMaintenanceHandler
from colony_sidecar.task_queue.handlers.registry import build_default_handlers

__all__ = [
    "InferenceHandler",
    "MonitoringHandler",
    "SystemMaintenanceHandler",
    "build_default_handlers",
]
