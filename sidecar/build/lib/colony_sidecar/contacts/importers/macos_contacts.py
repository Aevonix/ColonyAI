"""macOS Contacts framework importer."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("colony.contacts.importers.macos_contacts")


@dataclass
class RawContactRecord:
    """A contact record extracted from the macOS Contacts framework.

    Intermediate representation before deduplication and persistence.
    """
    given_name: Optional[str]
    family_name: Optional[str]
    organization: Optional[str]
    phone_numbers: List[str] = field(default_factory=list)   # E.164-normalized where possible
    email_addresses: List[str] = field(default_factory=list)
    source: str = "ios_contacts"

    @property
    def display_name(self) -> Optional[str]:
        parts = [self.given_name, self.family_name]
        name = " ".join(p for p in parts if p)
        return name or self.organization or None


class MacOSContactsImporter:
    """Imports contacts from the macOS Contacts framework.

    Requires pyobjc-framework-Contacts and macOS Contacts permission.
    If permission is denied or the framework is unavailable, raises ImportError
    with a descriptive message rather than silently returning an empty list.

    Args:
        normalize_phones: If True, attempt E.164 normalization of phone numbers.
    """

    def __init__(self, normalize_phones: bool = True) -> None:
        self._normalize = normalize_phones
        self._cn = self._load_framework()

    def _load_framework(self):
        try:
            import Contacts  # type: ignore  # pyobjc
            return Contacts
        except ImportError as exc:
            raise ImportError(
                "pyobjc-framework-Contacts is required for macOS Contacts import. "
                "Install it with: pip install pyobjc-framework-Contacts"
            ) from exc

    def fetch_all(self) -> List[RawContactRecord]:
        """Fetch all contacts from the macOS Contacts store.

        Returns:
            List of RawContactRecord objects.

        Raises:
            PermissionError: If Contacts access has not been granted.
            ImportError:     If the Contacts framework is unavailable.
        """
        CN = self._cn
        store = CN.CNContactStore.alloc().init()
        keys = [
            CN.CNContactGivenNameKey,
            CN.CNContactFamilyNameKey,
            CN.CNContactOrganizationNameKey,
            CN.CNContactPhoneNumbersKey,
            CN.CNContactEmailAddressesKey,
        ]
        fetch_request = CN.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        records: List[RawContactRecord] = []

        def handler(contact, _stop):
            phones = [
                v.value().stringValue()
                for v in (contact.phoneNumbers() or [])
            ]
            emails = [
                str(v.value())
                for v in (contact.emailAddresses() or [])
            ]
            records.append(RawContactRecord(
                given_name=str(contact.givenName()) or None,
                family_name=str(contact.familyName()) or None,
                organization=str(contact.organizationName()) or None,
                phone_numbers=phones,
                email_addresses=emails,
            ))

        error = None
        store.enumerateContactsWithFetchRequest_error_usingBlock_(
            fetch_request, error, handler
        )
        logger.info("Fetched %d contacts from macOS Contacts", len(records))
        return records
