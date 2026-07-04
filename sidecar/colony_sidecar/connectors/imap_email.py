"""IMAP email connector (read-only). Reference connector for item 2.

Reads recent message envelopes (headers + a short snippet) over IMAP and
normalizes them into "email" observations plus person/company entity hints.
Credentials are env-only (COLONY_CONNECTOR_IMAP_*). Never sends, never
deletes, never marks read (uses BODY.PEEK).
"""

from __future__ import annotations

import email
import logging
import re
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List

from colony_sidecar.connectors.base import Connector, EntityHint, Observation

logger = logging.getLogger(__name__)

# Consumer providers whose domain is NOT a company signal.
_GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
})


def _decode(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        parts = decode_header(raw)
        return "".join(
            (p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p)
            for p, enc in parts)
    except Exception:
        return str(raw)


def _company_from_email(addr: str) -> str:
    dom = (addr.rsplit("@", 1)[-1] if "@" in addr else "").lower()
    if not dom or dom in _GENERIC_DOMAINS:
        return ""
    base = dom.rsplit(".", 1)[0].split(".")[-1]  # acme from mail.acme.co.uk-ish
    return base.replace("-", " ").title() if base else ""


class IMAPEmailConnector(Connector):
    name = "imap"
    domain = "email"
    default_poll_secs = 900

    def normalize(self, messages: List[Dict[str, Any]]) -> List[Observation]:
        """Pure: [{message_id, from, to, subject, date, snippet}] -> Observations."""
        out: List[Observation] = []
        for m in messages:
            from_name, from_addr = parseaddr(m.get("from", ""))
            from_name = _decode(from_name) or from_addr
            subject = _decode(m.get("subject", ""))
            snippet = (m.get("snippet", "") or "")[:400]
            ts = self._ts(m.get("date"))
            entities: List[EntityHint] = []
            if from_name and "@" not in from_name and len(from_name.split()) <= 4:
                entities.append(EntityHint(kind="person", name=from_name,
                                           external_ids={"email": from_addr}))
            company = _company_from_email(from_addr)
            if company:
                entities.append(EntityHint(kind="company", name=company,
                                           external_ids={"domain": from_addr.rsplit('@', 1)[-1]}))
            text = (f"Email from {from_name} <{from_addr}> "
                    f"re: {subject}. {snippet}").strip()
            out.append(Observation(
                domain=self.domain,
                external_id=str(m.get("message_id") or f"{from_addr}:{subject}")[:200],
                ts=ts,
                payload={"from": from_addr, "from_name": from_name,
                         "to": m.get("to", ""), "subject": subject,
                         "snippet": snippet},
                entities=entities, text=text))
        return out

    @staticmethod
    def _ts(date_str: Any) -> float:
        import time
        if not date_str:
            return time.time()
        try:
            return parsedate_to_datetime(str(date_str)).timestamp()
        except Exception:
            return time.time()

    def _fetch(self) -> List[Dict[str, Any]]:
        import imaplib
        host = self.config.get("HOST")
        user = self.config.get("USER")
        password = self.config.get("PASSWORD")
        if not (host and user and password):
            return []
        port = self.config.get_int("PORT", 993)
        mailbox = self.config.get("MAILBOX", "INBOX")
        limit = self.config.get_int("MAX", 20)
        msgs: List[Dict[str, Any]] = []
        imap = imaplib.IMAP4_SSL(host, port)
        try:
            imap.login(user, password)
            imap.select(mailbox, readonly=True)
            typ, data = imap.search(None, "ALL")
            if typ != "OK":
                return []
            ids = data[0].split()[-limit:]
            for mid in reversed(ids):
                typ, msg_data = imap.fetch(
                    mid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT DATE)])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                hdr = email.message_from_bytes(msg_data[0][1])
                msgs.append({
                    "message_id": hdr.get("Message-ID", ""),
                    "from": hdr.get("From", ""), "to": hdr.get("To", ""),
                    "subject": hdr.get("Subject", ""), "date": hdr.get("Date", ""),
                    "snippet": ""})
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        return msgs

    def poll(self) -> List[Observation]:
        try:
            return self.normalize(self._fetch())
        except Exception:
            logger.debug("imap poll failed", exc_info=True)
            return []
