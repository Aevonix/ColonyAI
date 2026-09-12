"""PacoMind Contacts Management System.

Provides contact lifecycle management: storage, import, and privacy controls.
All contacts are Person nodes in the world model.

Quick start::

    from pacomind.contacts import ContactStore
    from pacomind.contacts.store import SQLiteContactStore
    from pacomind.contacts.config import ContactsConfig

    config = ContactsConfig(sqlite_path=":memory:")
    async with SQLiteContactStore(config) as store:
        contact = await store.create(display_name="Alice Chen", trust_tier="trusted")
        await store.add_handle(contact.contact_id, "email", "alice@example.com")
"""

from .store import ContactStore, SQLiteContactStore
from .importer import ContactImporter, SQLiteContactImporter
from .models import Contact, ContactHandle, MergeProposal, MergeAuditRecord
from .config import ContactsConfig

__all__ = [
    "ContactStore",
    "ContactImporter",
    "SQLiteContactStore",
    "SQLiteContactImporter",
    "Contact",
    "ContactHandle",
    "MergeProposal",
    "MergeAuditRecord",
    "ContactsConfig",
]
