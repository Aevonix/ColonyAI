"""Explicit valid-time and observation-time windows for memory recall."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

UTC = timezone.utc
_MONTHS = {name.casefold(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}
_DATE = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))?"
_MONTH_DATE = r"(?:" + "|".join(_MONTHS) + r")\s+\d{1,2},?\s+\d{4}"
_EVENT = re.compile(r"\b(footage|camera|observed|spotted|seen|saw|happened|recorded|arrived|visited)\b", re.I)


def utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def parse_source_date(expression: str, *, observed_at: str | None, timezone_name="UTC") -> str | None:
    """Resolve only explicit supported date text, anchored to source occurrence."""
    zone = ZoneInfo(timezone_name)
    value = expression.strip().casefold()
    try:
        if value in {"now", "today", "yesterday", "tomorrow"}:
            observed = utc_timestamp(observed_at)
            if observed is None:
                return None
            if value == "now":
                return observed.isoformat()
            local = observed.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
            local += timedelta(days={"yesterday": -1, "today": 0, "tomorrow": 1}[value])
            return local.astimezone(UTC).isoformat()
        if re.fullmatch(_DATE, expression, re.I):
            date = datetime.fromisoformat(expression.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=zone)
            return date.astimezone(UTC).isoformat()
        match = re.fullmatch(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
        if match and match[1] in _MONTHS:
            return datetime(int(match[3]), _MONTHS[match[1]], int(match[2]), tzinfo=zone).astimezone(UTC).isoformat()
    except ValueError:
        pass
    return None


@dataclass(frozen=True)
class MemoryTimeQuery:
    mode: str = "current"
    start: str | None = None
    end: str | None = None
    expression: str | None = None

    def accepts_observation(self, occurred_at: str | None) -> bool:
        if self.mode != "observed_range":
            return True
        stamp = utc_timestamp(occurred_at)
        if stamp is None:
            return False
        return (not self.start or stamp >= utc_timestamp(self.start)) and (not self.end or stamp < utc_timestamp(self.end))

    def accepts_claim(self, claim: dict) -> bool:
        if claim.get("retracted_by"):
            return False
        if self.mode == "unresolved_time":
            return False
        if self.mode == "observed_range":
            return self.accepts_observation(claim.get("event_at"))
        start, end = utc_timestamp(claim.get("valid_from")), utc_timestamp(claim.get("valid_to"))
        query_start, query_end = utc_timestamp(self.start), utc_timestamp(self.end)
        if self.mode == "valid_range":
            # A historical assertion with no known validity start cannot be
            # certified as true on an arbitrary earlier date.
            if start is None:
                return False
            return (query_end is None or start < query_end) and (end is None or query_start < end)
        if query_start:
            return (start is None or start <= query_start) and (end is None or query_start < end)
        return not claim.get("superseded_by")


def interpret_time_query(text: str, *, now: datetime, timezone_name="UTC") -> MemoryTimeQuery:
    """Calendar days differ from trailing windows and from source ingestion.

    A date without a clock covers that local calendar day. If a fact changes
    during the day, both intersecting validity intervals remain visible.
    """
    zone = ZoneInfo(timezone_name)
    text = text[:4096]
    mode = "observed_range" if _EVENT.search(text) else "valid_range"
    duration = re.search(r"\blast\s+(\d{1,3})\s+(hours?|days?)\b", text, re.I)
    if duration and mode == "observed_range":
        delta = timedelta(hours=int(duration[1])) if duration[2].lower().startswith("hour") else timedelta(days=int(duration[1]))
        return MemoryTimeQuery(mode, (now - delta).astimezone(UTC).isoformat(), now.astimezone(UTC).isoformat(), duration[0])
    # Do not quietly turn unsupported ranges/relative dates into a current
    # answer or select the first date in a multi-date question.
    unsupported = re.search(
        r"\b(before|after|between|until|through|last (?:week|month|year)|next (?:week|month|year)|"
        r"previous (?:week|month|year)|\d+ (?:weeks?|months?|years?) ago)\b", text, re.I)
    matches = list(re.finditer(_DATE + "|" + _MONTH_DATE + r"|\b(?:today|yesterday|tomorrow)\b", text, re.I))
    if unsupported or len(matches) > 1:
        return MemoryTimeQuery("unresolved_time", expression=text)
    match = re.search(_DATE + "|" + _MONTH_DATE + r"|\b(?:today|yesterday|tomorrow)\b", text, re.I)
    if match:
        expression = match[0]
        if (mode == "valid_range" and expression.lower() in {"today", "tomorrow"}
                and not re.search(r"\b(as of|valid|effective|held|was|were)\b", text, re.I)):
            # "What tea should I bring later today?" asks for a current
            # preference; it does not assert that an undated preference has a
            # certified historical validity interval.
            return MemoryTimeQuery("current", now.astimezone(UTC).isoformat())
        start = parse_source_date(expression, observed_at=now.isoformat(), timezone_name=timezone_name)
        if start:
            begin = utc_timestamp(start)
            if "T" in expression:
                end = (begin + timedelta(microseconds=1)).isoformat()
            else:
                end = (begin.astimezone(zone) + timedelta(days=1)).astimezone(UTC).isoformat()
            if re.search(r"\bsince\s+" + re.escape(expression), text, re.I):
                mode, end = "observed_range", now.astimezone(UTC).isoformat()
            return MemoryTimeQuery(mode, start, end, expression)
    return MemoryTimeQuery("current", now.astimezone(UTC).isoformat())


def filter_unstructured(rows: list[dict], query: MemoryTimeQuery) -> list[dict]:
    """Observation questions cannot use write time as evidence of occurrence."""
    if query.mode == "current":
        return rows
    if query.mode == "observed_range":
        return [dict(row, validity_status="source_occurrence_only")
                for row in rows if query.accepts_observation(row.get("occurred_at"))]
    # Source quotations alone do not establish when the asserted fact held.
    return [dict(row, validity_status="unknown") for row in rows]
