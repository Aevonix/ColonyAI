"""Email header contact extractor."""

from __future__ import annotations

import email.headerregistry
import logging
import re
from typing import List, Tuple

from .macos_contacts import RawContactRecord

logger = logging.getLogger("colony.contacts.importers.email_contacts")

# RFC 5322 named address pattern: "Display Name <user@example.com>"
_NAMED_ADDR = re.compile(r'^"?([^"<]+?)"?\s*<([^>]+)>\s*$')
_BARE_ADDR = re.compile(r'^[\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,}$')


def parse_address(addr_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse an RFC 5322 address into (display_name, email).

    Returns (None, None) if addr_str is not a valid address.
    """
    addr_str = addr_str.strip()
    m = _NAMED_ADDR.match(addr_str)
    if m:
        name = m.group(1).strip() or None
        email_addr = m.group(2).strip().lower()
        return name, email_addr
    if _BARE_ADDR.match(addr_str):
        return None, addr_str.lower()
    return None, None


def extract_from_headers(headers: List[str]) -> List[RawContactRecord]:
    """Extract RawContactRecord objects from a list of RFC 5322 address strings.

    Args:
        headers: List of address strings (From, To, CC, Reply-To values).

    Returns:
        List of RawContactRecord objects (one per unique email address found).
    """
    seen_emails: set = set()
    records: List[RawContactRecord] = []
    for addr_str in headers:
        # Handle comma-separated address lists
        for part in addr_str.split(","):
            display_name, email_addr = parse_address(part)
            if email_addr and email_addr not in seen_emails:
                seen_emails.add(email_addr)
                given_name = None
                family_name = None
                if display_name:
                    name_parts = display_name.split(None, 1)
                    given_name = name_parts[0] if name_parts else None
                    family_name = name_parts[1] if len(name_parts) > 1 else None
                records.append(RawContactRecord(
                    given_name=given_name,
                    family_name=family_name,
                    organization=None,
                    phone_numbers=[],
                    email_addresses=[email_addr],
                    source="email_header",
                ))
    logger.debug("Extracted %d contacts from email headers", len(records))
    return records


# Fix missing import
from typing import Optional  # noqa: E402
