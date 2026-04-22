"""SystemMaintenanceHandler — safe allow-listed maintenance jobs."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

from colony_sidecar.task_queue.handlers.base import JobHandler, Job, run_subprocess

logger = logging.getLogger(__name__)


class SystemMaintenanceHandler(JobHandler):
    """Run safe system maintenance commands (allow-listed only).

    Job payload:
        action (str): One of: "disk_cleanup", "log_rotate", "db_vacuum".
        target_path (str, optional): Path scoped to the colony data directory.

    Returns:
        {"actions_taken": list[str], "errors": list[str]}
    """

    ALLOWED_ACTIONS = {"disk_cleanup", "log_rotate", "db_vacuum"}

    async def execute(self, job: Job) -> Dict[str, Any]:
        action = job.payload.get("action")
        if action not in self.ALLOWED_ACTIONS:
            raise ValueError(
                f"Disallowed maintenance action: {action!r}. "
                f"Allowed: {sorted(self.ALLOWED_ACTIONS)}"
            )

        target_path: str | None = job.payload.get("target_path")
        actions_taken: List[str] = []
        errors: List[str] = []

        if action == "db_vacuum":
            if not target_path:
                errors.append("db_vacuum requires 'target_path' in payload")
            else:
                try:
                    import aiosqlite
                    async with aiosqlite.connect(target_path) as db:
                        await db.execute("VACUUM")
                    actions_taken.append(f"vacuumed {target_path}")
                    logger.info("SystemMaintenance: vacuumed %s", target_path)
                except ImportError:
                    errors.append(
                        "db_vacuum requires 'aiosqlite'. Install with: pip install aiosqlite"
                    )
                except Exception as exc:
                    errors.append(f"db_vacuum failed: {exc}")
                    logger.warning(
                        "SystemMaintenance: db_vacuum error on %s: %s",
                        target_path, exc,
                    )

        elif action == "disk_cleanup":
            path = Path(target_path) if target_path else Path.home() / ".colony" / "tmp"
            try:
                cleaned = 0
                if path.is_dir():
                    for item in path.iterdir():
                        try:
                            if item.is_file():
                                item.unlink()
                                cleaned += 1
                            elif item.is_dir():
                                shutil.rmtree(item)
                                cleaned += 1
                        except Exception as exc:
                            errors.append(f"Could not remove {item}: {exc}")
                actions_taken.append(f"disk_cleanup: removed {cleaned} items from {path}")
                logger.info("SystemMaintenance: disk_cleanup removed %d items from %s", cleaned, path)
            except Exception as exc:
                errors.append(f"disk_cleanup failed: {exc}")
                logger.warning("SystemMaintenance: disk_cleanup error: %s", exc)

        elif action == "log_rotate":
            path = Path(target_path) if target_path else Path.home() / ".colony" / "logs"
            try:
                rotated = 0
                if path.is_dir():
                    for log_file in path.glob("*.log"):
                        rotated_name = log_file.with_suffix(".log.bak")
                        try:
                            log_file.rename(rotated_name)
                            rotated += 1
                        except Exception as exc:
                            errors.append(f"Could not rotate {log_file}: {exc}")
                actions_taken.append(f"log_rotate: rotated {rotated} log files in {path}")
                logger.info("SystemMaintenance: log_rotate rotated %d files in %s", rotated, path)
            except Exception as exc:
                errors.append(f"log_rotate failed: {exc}")
                logger.warning("SystemMaintenance: log_rotate error: %s", exc)

        return {"actions_taken": actions_taken, "errors": errors}
