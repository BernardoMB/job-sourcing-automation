"""Repeating meetings in a calendar file (RRULE), turned into the single meetings inside a time window.

Covers what Outlook and Google write for meetings: DAILY, WEEKLY (with BYDAY), MONTHLY (BYMONTHDAY, or
BYDAY such as 2TU or -1FR), YEARLY, with INTERVAL, COUNT, UNTIL, EXDATE (cancelled occurrences), and
RECURRENCE-ID (one occurrence moved or renamed). Times repeat at the same wall-clock time in the meeting's
own time zone, so a 9:00 meeting stays at 9:00 across daylight-saving changes.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from steward.invites import _zone
from steward.timeutil import parse_iso, to_iso

DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MAX_STEPS = 5000  # never loop forever on a rule nobody can read


def _rule(text: str) -> dict[str, str]:
    return {k.upper(): v for k, _, v in (p.partition("=") for p in text.split(";")) if k}


def _until(value: str, zone) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 8:  # a date: the whole day counts
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=zone) + timedelta(days=1)
        d = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
        return d.replace(tzinfo=timezone.utc) if value.endswith("Z") else d.replace(tzinfo=zone)
    except ValueError:
        return None


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int | None:
    """Day of the month of the n-th (or, negative, n-th last) given weekday."""
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
            if calendar.weekday(year, month, d) == weekday]
    try:
        return days[n - 1] if n > 0 else days[n]
    except IndexError:
        return None


def _candidates(start: datetime, rule: dict[str, str]) -> Iterator[datetime]:
    """Every occurrence of the rule from the first one on, in order (COUNT and UNTIL applied by the caller)."""
    freq, step = rule.get("FREQ", "").upper(), max(1, int(rule.get("INTERVAL") or 1))
    byday = [d.strip().upper() for d in rule.get("BYDAY", "").split(",") if d.strip()]
    bymonthday = [int(x) for x in rule.get("BYMONTHDAY", "").split(",") if x.strip().lstrip("-").isdigit()]
    wall = (start.hour, start.minute, start.second)

    def at(y: int, m: int, d: int) -> datetime:
        return start.replace(year=y, month=m, day=d, hour=wall[0], minute=wall[1], second=wall[2])

    for k in range(MAX_STEPS):
        if freq == "DAILY":
            yield start + timedelta(days=k * step)
        elif freq == "WEEKLY":
            monday = (start - timedelta(days=start.weekday())).date() + timedelta(weeks=k * step)
            for wd in sorted({DAYS.index(d[-2:]) for d in byday if d[-2:] in DAYS} or {start.weekday()}):
                day = monday + timedelta(days=wd)
                yield at(day.year, day.month, day.day)
        elif freq == "MONTHLY":
            y, m = divmod(start.month - 1 + k * step, 12)
            y, m = start.year + y, m + 1
            last = calendar.monthrange(y, m)[1]
            days: list[int] = []
            if byday:
                for d in byday:
                    wd = DAYS.index(d[-2:]) if d[-2:] in DAYS else None
                    if wd is None:
                        continue
                    if d[:-2]:
                        n = _nth_weekday(y, m, wd, int(d[:-2]))
                        days += [n] if n else []
                    else:
                        days += [x for x in range(1, last + 1) if calendar.weekday(y, m, x) == wd]
            else:
                for d in bymonthday or [start.day]:
                    day = d if d > 0 else last + d + 1
                    if 1 <= day <= last:
                        days.append(day)
            for day in sorted(set(days)):
                yield at(y, m, day)
        elif freq == "YEARLY":
            y = start.year + k * step
            if start.day <= calendar.monthrange(y, start.month)[1]:
                yield at(y, start.month, start.day)
        else:
            yield start
            return


def occurrences(event: dict[str, Any], window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    """The single meetings of one event (repeating or not) that touch the window, each with its own start/end."""
    start, end = parse_iso(event["start"]), parse_iso(event["end"])
    if start is None or end is None:
        return []
    if not event.get("rrule"):
        return [event] if start < window_end and end > window_start else []
    zone = _zone(event.get("tzid", "")) or ZoneInfo("UTC")
    local = start.astimezone(zone)
    length = end - start
    rule = _rule(event["rrule"])
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = _until(rule.get("UNTIL", ""), zone)
    skip = {parse_iso(x) for x in event.get("exdates") or []}
    out, n = [], 0
    for when in _candidates(local, rule):
        if when < local:
            continue  # BYDAY days earlier in the first week than the first meeting
        if (until and when > until) or (count is not None and n >= count) or when >= window_end:
            break
        n += 1
        if when + length > window_start and when not in skip:
            out.append({**event, "start": to_iso(when), "end": to_iso(when + length), "rrule": "",
                        "occurrence": to_iso(when)})
    return out


def expand(events: list[dict[str, Any]], window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    """A whole calendar file as single meetings in the window: repeats expanded, cancelled and moved ones applied."""
    moved: dict[tuple[str, Any], dict[str, Any]] = {}
    for e in events:
        if e.get("recurrence_id"):
            moved[(e["uid"], parse_iso(e["recurrence_id"]))] = e
    out = []
    for e in events:
        if e.get("recurrence_id"):
            continue
        for occ in occurrences(e, window_start, window_end):
            key = (e["uid"], parse_iso(occ.get("occurrence") or occ["start"]))
            if key in moved:
                continue  # replaced by its own entry below
            out.append(occ)
    for (uid, original), e in moved.items():
        s, f = parse_iso(e["start"]), parse_iso(e["end"])
        if s and f and s < window_end and f > window_start:
            out.append({**e, "occurrence": to_iso(original)})
    return [e for e in out if e.get("method") != "cancel"]
