"""Scheduling brain: working hours, buffers, notice, avoid-windows, slot finding."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from steward.timeutil import parse_iso, tz

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PREFERRED_TIMES = [time(10, 0), time(14, 0), time(11, 0), time(15, 30), time(9, 30)]


def parse_hhmm(text: str) -> time:
    h, m = text.strip().split(":")
    return time(int(h), int(m))


def parse_avoid(spec: str) -> tuple[int | None, time, time] | None:
    """'Fri 15:00-18:00' or '12:00-13:00' (every day)."""
    parts = spec.strip().split()
    try:
        if len(parts) == 2:
            day = DAY_NAMES.index(parts[0][:3].lower())
            rng = parts[1]
        else:
            day, rng = None, parts[0]
        a, b = rng.split("-")
        return day, parse_hhmm(a), parse_hhmm(b)
    except (ValueError, IndexError):
        return None


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def conflicts(busy: list[dict[str, Any]], start: datetime, end: datetime, buffer_minutes: int = 0) -> list[dict[str, Any]]:
    pad = timedelta(minutes=buffer_minutes)
    out = []
    for b in busy:
        bs, be = parse_iso(b["start"]), parse_iso(b["end"])
        if bs is None or be is None:
            continue
        if _overlaps(start - pad, end + pad, bs, be):
            out.append(b)
    return out


def within_working_hours(start: datetime, end: datetime, tz_name: str, working_days: list[int],
                         work_start: str, work_end: str) -> bool:
    zone = tz(tz_name)
    ls, le = start.astimezone(zone), end.astimezone(zone)
    if ls.weekday() not in working_days or ls.date() != le.date():
        return False
    return parse_hhmm(work_start) <= ls.time() and le.time() <= parse_hhmm(work_end)


def in_avoid_window(start: datetime, end: datetime, tz_name: str, avoid: list[str]) -> bool:
    zone = tz(tz_name)
    ls, le = start.astimezone(zone), end.astimezone(zone)
    for spec in avoid:
        parsed = parse_avoid(spec)
        if not parsed:
            continue
        day, a, b = parsed
        if day is not None and ls.weekday() != day:
            continue
        win_s = ls.replace(hour=a.hour, minute=a.minute, second=0, microsecond=0)
        win_e = ls.replace(hour=b.hour, minute=b.minute, second=0, microsecond=0)
        if _overlaps(ls, le, win_s, win_e):
            return True
    return False


def evaluate_slot(busy: list[dict[str, Any]], start: datetime, end: datetime, *, tz_name: str,
                  working_days: list[int], work_start: str, work_end: str, buffer_minutes: int,
                  min_notice_hours: int, avoid: list[str], now: datetime) -> dict[str, Any]:
    problems = []
    hits = conflicts(busy, start, end, buffer_minutes)
    if hits:
        problems.append("calendar conflict")
    if not within_working_hours(start, end, tz_name, working_days, work_start, work_end):
        problems.append("outside working hours")
    if in_avoid_window(start, end, tz_name, avoid):
        problems.append("inside a blocked window")
    if start < now + timedelta(hours=min_notice_hours):
        problems.append("too short notice")
    if start < now:
        problems = ["in the past"]
    return {"ok": not problems, "problems": problems, "conflicts": hits}


def find_slots(busy: list[dict[str, Any]], window_start: datetime, window_end: datetime, *, duration_minutes: int,
               tz_name: str, working_days: list[int], work_start: str, work_end: str, buffer_minutes: int,
               min_notice_hours: int, avoid: list[str], max_slots: int, now: datetime,
               step_minutes: int = 30, spread_days: bool = True) -> list[dict[str, str]]:
    zone = tz(tz_name)
    duration = timedelta(minutes=duration_minutes)
    earliest = max(window_start, now + timedelta(hours=min_notice_hours))
    day = earliest.astimezone(zone).date()
    last_day = window_end.astimezone(zone).date()
    ws, we = parse_hhmm(work_start), parse_hhmm(work_end)
    per_day: list[list[datetime]] = []
    while day <= last_day:
        if day.weekday() in working_days:
            cursor = datetime.combine(day, ws, tzinfo=zone)
            end_of_day = datetime.combine(day, we, tzinfo=zone)
            free: list[datetime] = []
            while cursor + duration <= end_of_day:
                if cursor >= earliest and cursor + duration <= window_end:
                    check = evaluate_slot(busy, cursor, cursor + duration, tz_name=tz_name, working_days=working_days,
                                          work_start=work_start, work_end=work_end, buffer_minutes=buffer_minutes,
                                          min_notice_hours=min_notice_hours, avoid=avoid, now=now)
                    if check["ok"]:
                        free.append(cursor)
                cursor += timedelta(minutes=step_minutes)
            if free:
                per_day.append(free)
        day += timedelta(days=1)

    chosen: list[datetime] = []
    if spread_days:
        for i, free in enumerate(per_day):
            if len(chosen) >= max_slots:
                break
            pref = PREFERRED_TIMES[i % len(PREFERRED_TIMES)]
            pick = next((f for f in free if f.time() >= pref), free[0])
            chosen.append(pick)
    for free in per_day:
        for f in free:
            if len(chosen) >= max_slots:
                break
            if f not in chosen and all(abs((f - c).total_seconds()) >= 3600 for c in chosen):
                chosen.append(f)
    chosen.sort()
    return [{"start": c.isoformat(), "end": (c + duration).isoformat()} for c in chosen[:max_slots]]
