"""Contact permission system."""
from dataclasses import dataclass
from typing import Dict, Set, Optional
from datetime import datetime
from enum import Enum


class PermissionLevel(str, Enum):
    """Permission levels for contact access."""
    NONE = "none"
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


@dataclass
class ContactPermission:
    """Permission record for a contact."""
    contact_id: str
    level: PermissionLevel
    granted_by: str  # owner or system
    granted_at: datetime
    expires_at: Optional[datetime] = None
    scope: Set[str] = None  # Specific data types allowed

    def __post_init__(self):
        if self.scope is None:
            self.scope = set()


class PermissionsManager:
    """Manage contact permissions for data access."""

    def __init__(self, graph: "ColonyGraph"):
        self.graph = graph
        self._cache: Dict[str, ContactPermission] = {}

    async def get_permission(self, contact_id: str) -> ContactPermission:
        """Get permission level for a contact."""
        if contact_id in self._cache:
            return self._cache[contact_id]

        # Load from graph
        try:
            perm_data = await self.graph.get_contact_permission(contact_id)
            if perm_data:
                perm = ContactPermission(
                    contact_id=contact_id,
                    level=PermissionLevel(perm_data.get("level", "none")),
                    granted_by=perm_data.get("granted_by", "system"),
                    granted_at=datetime.fromisoformat(perm_data["granted_at"]),
                    expires_at=datetime.fromisoformat(perm_data["expires_at"]) if perm_data.get("expires_at") else None,
                    scope=set(perm_data.get("scope", [])),
                )
                self._cache[contact_id] = perm
                return perm
        except Exception:
            pass

        # Default: no permission
        return ContactPermission(
            contact_id=contact_id,
            level=PermissionLevel.NONE,
            granted_by="system",
            granted_at=datetime.now(),
        )

    async def grant(
        self,
        contact_id: str,
        level: PermissionLevel,
        granted_by: str = "owner",
        scope: Optional[Set[str]] = None,
        expires_at: Optional[datetime] = None,
    ) -> ContactPermission:
        """Grant permission to a contact."""
        perm = ContactPermission(
            contact_id=contact_id,
            level=level,
            granted_by=granted_by,
            granted_at=datetime.now(),
            expires_at=expires_at,
            scope=scope or set(),
        )

        # Persist to graph
        await self.graph.set_contact_permission(
            contact_id=contact_id,
            level=level.value,
            granted_by=granted_by,
            granted_at=perm.granted_at.isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
            scope=list(perm.scope),
        )

        self._cache[contact_id] = perm
        return perm

    async def revoke(self, contact_id: str) -> None:
        """Revoke all permissions from a contact."""
        await self.graph.delete_contact_permission(contact_id)
        self._cache.pop(contact_id, None)

    async def check(self, contact_id: str, required_level: PermissionLevel) -> bool:
        """Check if contact has required permission level."""
        perm = await self.get_permission(contact_id)

        # Check expiration
        if perm.expires_at and datetime.now() > perm.expires_at:
            return False

        # Check level hierarchy
        levels = [PermissionLevel.NONE, PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.ADMIN]
        perm_idx = levels.index(perm.level)
        req_idx = levels.index(required_level)

        return perm_idx >= req_idx

    async def can_read(self, contact_id: str, data_type: Optional[str] = None) -> bool:
        """Check read permission, optionally scoped to data type."""
        if not await self.check(contact_id, PermissionLevel.READ):
            return False

        if data_type:
            perm = await self.get_permission(contact_id)
            # If scope is empty, all data types allowed
            if perm.scope and data_type not in perm.scope:
                return False

        return True

    async def can_write(self, contact_id: str) -> bool:
        """Check write permission."""
        return await self.check(contact_id, PermissionLevel.WRITE)

    async def list_with_permission(self, level: PermissionLevel) -> list:
        """List all contacts with at least the specified permission level."""
        # Would query graph for all permissions >= level
        results = []
        for contact_id, perm in self._cache.items():
            levels = [PermissionLevel.NONE, PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.ADMIN]
            if levels.index(perm.level) >= levels.index(level):
                results.append(contact_id)
        return results
