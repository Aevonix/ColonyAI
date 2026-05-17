"""Operational hygiene executor skill.

Handles backups, log rotation, disk space, and scheduled maintenance.
"""

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class OperationalHygieneSkill(InitiativeExecutorSkill):
    """Skill for operational maintenance tasks."""

    skill_name = "operational_hygiene"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        entity_id = initiative.entity_id or "unknown"
        entity_type = initiative.entity_type or "maintenance"
        self._log("info", "Operational task: %s (%s)", entity_id, entity_type)

        if entity_type == "backup":
            return await self._handle_backup(entity_id, initiative.trigger_data)
        elif entity_type == "log_rotation":
            return await self._handle_log_rotation(entity_id, initiative.trigger_data)
        elif entity_type == "disk_cleanup":
            return await self._handle_disk_cleanup(entity_id, initiative.trigger_data)
        elif entity_type == "cache_refresh":
            return await self._handle_cache_refresh(entity_id, initiative.trigger_data)
        else:
            self._log("warning", "Unknown operational task: %s", entity_type)
            return ExecutionResult.NO_ACTION

    async def _handle_backup(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Trigger a database backup."""
        self._log("info", "Triggering backup for: %s", entity_id)

        backup_dir = Path(os.path.expanduser("~/.colony/backups"))
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Backup SQLite stores
        stores = ["colony.db", "colony-contacts.db"]
        backed_up = []
        for store_name in stores:
            source = Path(os.path.expanduser(f"~/.colony/data/{store_name}"))
            if source.exists():
                dest = backup_dir / f"{store_name}.{timestamp}.bak"
                try:
                    shutil.copy2(source, dest)
                    backed_up.append(store_name)
                except Exception as e:
                    self._log("warning", "Failed to backup %s: %s", store_name, e)

        if backed_up:
            self._log("info", "Backed up %d stores", len(backed_up))
            # Clean up old backups (keep last 7)
            await self._cleanup_old_backups(backup_dir, keep=7)
            return ExecutionResult.AUTO_FIXED
        else:
            return ExecutionResult.NO_ACTION

    async def _cleanup_old_backups(self, backup_dir: Path, keep: int = 7) -> None:
        """Remove old backups, keeping only the most recent N."""
        try:
            backups = sorted(
                backup_dir.glob("*.bak"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old_backup in backups[keep:]:
                old_backup.unlink()
                self._log("debug", "Removed old backup: %s", old_backup.name)
        except Exception as e:
            self._log("warning", "Failed to cleanup old backups: %s", e)

    async def _handle_log_rotation(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Rotate log files if they exceed threshold."""
        self._log("info", "Rotating logs for: %s", entity_id)

        log_dir = Path(os.path.expanduser("~/.colony/logs"))
        if not log_dir.exists():
            return ExecutionResult.NO_ACTION

        threshold_mb = trigger_data.get("threshold_mb", 100)
        rotated = []

        for log_file in log_dir.glob("*.log"):
            try:
                size_mb = log_file.stat().st_size / (1024 * 1024)
                if size_mb > threshold_mb:
                    # Compress and rotate
                    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    rotated_name = f"{log_file.stem}.{timestamp}.log.gz"
                    rotated_path = log_dir / rotated_name

                    import gzip
                    with open(log_file, "rb") as f_in:
                        with gzip.open(rotated_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)

                    # Truncate original
                    log_file.write_text("")
                    rotated.append(str(log_file.name))
                    self._log("info", "Rotated %s (%.1f MB)", log_file.name, size_mb)
            except Exception as e:
                self._log("warning", "Failed to rotate %s: %s", log_file.name, e)

        if rotated:
            return ExecutionResult.AUTO_FIXED
        return ExecutionResult.NO_ACTION

    async def _handle_disk_cleanup(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Clean up temporary files and caches."""
        self._log("info", "Cleaning disk for: %s", entity_id)

        cleaned_bytes = 0
        paths_to_clean = [
            Path(os.path.expanduser("~/.colony/cache")),
            Path(os.path.expanduser("~/.colony/tmp")),
        ]

        for path in paths_to_clean:
            if path.exists():
                try:
                    for item in path.iterdir():
                        if item.is_file():
                            age = datetime.now(timezone.utc).timestamp() - item.stat().st_mtime
                            # Remove files older than 7 days
                            if age > 7 * 24 * 3600:
                                cleaned_bytes += item.stat().st_size
                                item.unlink()
                        elif item.is_dir():
                            age = datetime.now(timezone.utc).timestamp() - item.stat().st_mtime
                            if age > 7 * 24 * 3600:
                                shutil.rmtree(item)
                except Exception as e:
                    self._log("warning", "Failed to clean %s: %s", path, e)

        if cleaned_bytes > 0:
            self._log("info", "Cleaned %.1f MB", cleaned_bytes / (1024 * 1024))
            return ExecutionResult.AUTO_FIXED
        return ExecutionResult.NO_ACTION

    async def _handle_cache_refresh(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Refresh stale caches."""
        self._log("info", "Refreshing cache: %s", entity_id)

        if entity_id == "model_weights":
            # Trigger model weight refresh via event bus
            if self.events:
                try:
                    await self.events.publish("model_refresh_requested", {
                        "reason": "stale_cache",
                    })
                    return ExecutionResult.AUTO_FIXED
                except Exception as e:
                    self._log("warning", "Failed to request model refresh: %s", e)

        return ExecutionResult.NO_ACTION
