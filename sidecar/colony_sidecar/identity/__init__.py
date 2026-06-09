"""Identity normalization service (v0.16.0).

Maps between the identifier formats Colony uses for a person:
contact-store CID, Neo4j Person node ID, display name, and platform
handles (email, phone, etc.).
"""

from colony_sidecar.identity.resolver import (
    IdentityResolver,
    OwnerIdentityError,
    get_identity_resolver,
    get_owner_contact_id,
    reset_identity_resolver,
)

__all__ = [
    "IdentityResolver",
    "OwnerIdentityError",
    "get_identity_resolver",
    "get_owner_contact_id",
    "reset_identity_resolver",
]
