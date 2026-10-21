"""Time helpers. Everything is stored as timezone-aware ISO 8601 in UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return to_iso(utcnow())


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def parse_iso(value: str | datetime | None, default_tz: str = "UTC") -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(default_tz))
    return dt


def to_iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def fmt_local(dt: datetime | str | None, tz_name: str, with_date: bool = True) -> str:
    d = parse_iso(dt) if not isinstance(dt, datetime) else dt
    if d is None:
        return ""
    local = d.astimezone(tz(tz_name))
    hour = local.strftime("%I:%M %p").lstrip("0")
    zone = local.strftime("%Z")
    if with_date:
        return f"{local.strftime('%a %b')} {local.day}, {hour} {zone}".strip()
    return f"{hour} {zone}".strip()


def fmt_range(start: datetime | str, end: datetime | str, tz_name: str) -> str:
    s = parse_iso(start) if not isinstance(start, datetime) else start
    e = parse_iso(end) if not isinstance(end, datetime) else end
    if s is None or e is None:
        return ""
    ls, le = s.astimezone(tz(tz_name)), e.astimezone(tz(tz_name))
    start_txt = f"{ls.strftime('%a %b')} {ls.day}, {ls.strftime('%I:%M %p').lstrip('0')}"
    end_txt = le.strftime("%I:%M %p").lstrip("0")
    return f"{start_txt} - {end_txt} {ls.strftime('%Z')}"


def ago(value: str | None) -> str:
    d = parse_iso(value)
    if d is None:
        return ""
    delta = utcnow() - d
    secs = int(delta.total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def days_between(a: str | None, b: datetime | None = None) -> float:
    d = parse_iso(a)
    if d is None:
        return 0.0
    return ((b or utcnow()) - d) / timedelta(days=1)
