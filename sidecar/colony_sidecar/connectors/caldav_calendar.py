"""Calendar connector (read-only): pulls an iCalendar (.ics) feed and
normalizes VEVENTs into "calendar" observations + Event/person entities.

An ICS feed URL (Google/CalDAV/Exchange all expose one) is the
dependency-light pull path; a full CalDAV REPORT or an OAuth API token is the
deployment option, documented in the handoff. Credentials env-only
(COLONY_CONNECTOR_CALENDAR_*). Read-only: never creates or edits events.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from colony_sidecar.connectors.base import Connector, EntityHint, Observation

logger = logging.getLogger(__name__)

_UNFOLD = re.compile(r"\r?\n[ \t]")


def _parse_ics(ics_text: str) -> List[Dict[str, Any]]:
    """Minimal VEVENT parser (stdlib): returns per-event property dicts."""
    text = _UNFOLD.sub("", ics_text or "")  # unfold continued lines
    events: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    in_event = False
    for line in text.splitlines():
        if line.startswith("BEGIN:VEVENT"):
            in_event, cur = True, {"attendees": []}
            continue
        if line.startswith("END:VEVENT"):
            if in_event:
                events.append(cur)
            in_event = False
            continue
        if not in_event or ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.split(";", 1)[0].upper()
        if key == "ATTENDEE":
            cn = re.search(r"CN=([^;:]+)", name)
            cur["attendees"].append(cn.group(1) if cn else value.replace("mailto:", ""))
        elif key in ("SUMMARY", "UID", "LOCATION", "DTSTART", "DTEND",
                     "ORGANIZER", "DESCRIPTION"):
            cur[key.lower()] = value
            if key == "ORGANIZER":
                cn = re.search(r"CN=([^;:]+)", name)
                if cn:
                    cur["organizer_name"] = cn.group(1)
    return events


def _ics_ts(value: str) -> float:
    if not value:
        return time.time()
    v = value.strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return time.time()


class CalendarConnector(Connector):
    name = "calendar"
    domain = "calendar"
    default_poll_secs = 1800

    def normalize(self, events: List[Dict[str, Any]]) -> List[Observation]:
        out: List[Observation] = []
        for ev in events:
            summary = ev.get("summary", "").strip()
            uid = ev.get("uid", "") or summary
            location = ev.get("location", "").strip()
            attendees = [a.strip() for a in ev.get("attendees", []) if a.strip()]
            organizer = ev.get("organizer_name", "").strip()
            ts = _ics_ts(ev.get("dtstart", ""))
            entities: List[EntityHint] = []
            if summary:
                entities.append(EntityHint(kind="event", name=summary,
                                           external_ids={"uid": uid}))
            for person in ([organizer] if organizer else []) + attendees:
                if person and "@" not in person and len(person.split()) <= 4:
                    entities.append(EntityHint(kind="person", name=person))
            if location and not location.startswith("http"):
                entities.append(EntityHint(kind="location", name=location))
            who = ", ".join([organizer] + attendees).strip(", ")
            text = (f"Calendar event '{summary}'"
                    + (f" at {location}" if location else "")
                    + (f" with {who}" if who else "")).strip()
            out.append(Observation(
                domain=self.domain, external_id=str(uid)[:200], ts=ts,
                payload={"summary": summary, "location": location,
                         "attendees": attendees, "organizer": organizer,
                         "start": ev.get("dtstart", ""), "end": ev.get("dtend", "")},
                entities=entities, text=text))
        return out

    def _fetch(self) -> List[Dict[str, Any]]:
        import urllib.request
        url = self.config.get("ICS_URL")
        if not url:
            return []
        req = urllib.request.Request(url, method="GET")
        user = self.config.get("USER")
        password = self.config.get("PASSWORD")
        if user and password:
            import base64
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _parse_ics(resp.read().decode("utf-8", "replace"))

    def poll(self) -> List[Observation]:
        try:
            limit = self.config.get_int("MAX", 50)
            return self.normalize(self._fetch()[:limit])
        except Exception:
            logger.debug("calendar poll failed", exc_info=True)
            return []
