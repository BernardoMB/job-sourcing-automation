"""Meeting invitations inside an email: .ics attachments and "Add to Calendar" links.

Turns them into exact times (ISO 8601 in UTC) so a recruiter's invitation can go
on your calendar without guessing. Reads the time zones Outlook writes (Windows
names such as "Pacific Standard Time"), and the parameters of Google Calendar,
Outlook, and Yahoo "Add to calendar" links. Links behind tracking redirects
carry no times and are ignored; nothing here ever fetches a URL.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from steward.timeutil import to_iso

ADD_TO_CALENDAR = re.compile(r"add to (my |your )?calendar|a[ñn]adir (al|a mi) calendario|agregar al calendario",
                             re.IGNORECASE)
URL = re.compile(r"""https?://[^\s"'<>]+""")
HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# The Windows time zone names Outlook and Teams put in TZID, for the zones recruiters are likely to use.
WINDOWS_ZONES = {
    "Pacific Standard Time": "America/Los_Angeles", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York", "US Eastern Standard Time": "America/Indianapolis",
    "Atlantic Standard Time": "America/Halifax", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Newfoundland Standard Time": "America/St_Johns",
    "Canada Central Standard Time": "America/Regina", "Central Standard Time (Mexico)": "America/Mexico_City",
    "Mountain Standard Time (Mexico)": "America/Mazatlan", "Pacific Standard Time (Mexico)": "America/Tijuana",
    "Eastern Standard Time (Mexico)": "America/Cancun", "Central America Standard Time": "America/Guatemala",
    "SA Pacific Standard Time": "America/Bogota", "SA Western Standard Time": "America/La_Paz",
    "SA Eastern Standard Time": "America/Cayenne", "Venezuela Standard Time": "America/Caracas",
    "Pacific SA Standard Time": "America/Santiago", "Argentina Standard Time": "America/Buenos_Aires",
    "E. South America Standard Time": "America/Sao_Paulo", "Montevideo Standard Time": "America/Montevideo",
    "Paraguay Standard Time": "America/Asuncion", "Peru Standard Time": "America/Lima",
    "UTC": "UTC", "Coordinated Universal Time": "UTC", "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik", "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris", "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul", "Israel Standard Time": "Asia/Jerusalem",
    "Egypt Standard Time": "Africa/Cairo", "South Africa Standard Time": "Africa/Johannesburg",
    "W. Central Africa Standard Time": "Africa/Lagos", "Arabian Standard Time": "Asia/Dubai",
    "Pakistan Standard Time": "Asia/Karachi", "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok", "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai", "Taipei Standard Time": "Asia/Taipei", "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul", "W. Australia Standard Time": "Australia/Perth",
    "AUS Eastern Standard Time": "Australia/Sydney", "New Zealand Standard Time": "Pacific/Auckland",
}


def _zone(name: str) -> ZoneInfo | None:
    name = (name or "").strip().strip('"')
    for candidate in (name, WINDOWS_ZONES.get(name, "")):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except Exception:  # noqa: BLE001 - unknown names fall through
                pass
    m = re.search(r"([A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)$", name)  # e.g. /mozilla.org/.../America/New_York
    if m:
        try:
            return ZoneInfo(m.group(1))
        except Exception:  # noqa: BLE001
            return None
    return None


def _offset(value: str) -> timezone | None:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", (value or "").strip())
    if not m:
        return None
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


class _Custom:
    """A VTIMEZONE Job Sourcer cannot name (e.g. Outlook's "Customized Time Zone"): read its own offsets."""

    def __init__(self, standard: timezone | None, daylight: timezone | None, dst_months: tuple[int, int] | None):
        self.standard, self.daylight, self.dst_months = standard, daylight, dst_months

    def at(self, naive: datetime) -> timezone | None:
        if self.daylight and self.dst_months:
            begin, end = self.dst_months  # months daylight time starts and ends in
            inside = begin < naive.month < end if begin < end else (naive.month > begin or naive.month < end)
            if inside:
                return self.daylight
        return self.standard


def _unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n")).split("\n")


def _split(line: str) -> tuple[str, dict[str, str], str]:
    """NAME;PARAM=x;PARAM="y:z":value  ->  (NAME, {PARAM: x, ...}, value). Colons inside quotes are not separators."""
    quoted, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = i
            break
    if cut < 0:
        return "", {}, ""
    head, value = line[:cut], line[cut + 1:]
    name, *params = head.split(";")
    out = {}
    for p in params:
        k, _, v = p.partition("=")
        out[k.upper()] = v.strip('"')
    return name.upper(), out, value


def _text(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def _when(value: str, params: dict[str, str], customs: dict[str, _Custom], default_tz: str) -> datetime | None:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return None  # all-day: not a meeting
    m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z)?", value)
    if not m:
        return None
    naive = datetime.strptime(m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S")
    if m.group(4):
        return naive.replace(tzinfo=timezone.utc)
    tzid = params.get("TZID", "")
    zone = _zone(tzid) if tzid else None
    if zone is None and tzid in customs:
        fixed = customs[tzid].at(naive)
        return naive.replace(tzinfo=fixed) if fixed else None
    if zone is None and tzid:
        return None  # a time zone nobody can read: better no event than one at the wrong hour
    return naive.replace(tzinfo=zone or ZoneInfo(default_tz))


def _duration(value: str) -> timedelta | None:
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return None
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_ics(text: str, default_tz: str = "UTC") -> list[dict[str, Any]]:
    """The VEVENTs of an iCalendar file, as invites with UTC start/end."""
    lines = _unfold(text or "")
    method = ""
    customs: dict[str, _Custom] = {}
    # Pass 1: VTIMEZONE definitions with names nobody else knows.
    stack: list[str] = []
    tzid, part, parts = "", "", {}
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() in ("STANDARD", "DAYLIGHT"):
                part = value.upper()
                parts[part] = {}
        elif name == "END":
            if value.upper() == "VTIMEZONE" and tzid:
                std, dl = parts.get("STANDARD", {}), parts.get("DAYLIGHT", {})
                months = None
                if std.get("month") and dl.get("month"):
                    months = (dl["month"], std["month"])
                customs[tzid] = _Custom(_offset(std.get("to", "")), _offset(dl.get("to", "")), months)
                tzid, parts = "", {}
            if stack:
                stack.pop()
            part = ""
        elif stack and stack[-1] == "VTIMEZONE" and name == "TZID":
            tzid = value.strip()
        elif part and name == "TZOFFSETTO":
            parts[part]["to"] = value
        elif part and name == "RRULE":
            m = re.search(r"BYMONTH=(\d+)", value)
            if m:
                parts[part]["month"] = int(m.group(1))
        elif name == "METHOD" and not stack[1:]:
            method = value.strip().upper()
    # Pass 2: the events themselves.
    out: list[dict[str, Any]] = []
    stack, ev = [], None
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VEVENT":
                ev = {"params": {}, "values": {}}
            continue
        if name == "END":
            if value.upper() == "VEVENT" and ev is not None:
                inv = _event(ev, method, customs, default_tz)
                if inv:
                    out.append(inv)
                ev = None
            if stack:
                stack.pop()
            continue
        if ev is not None and stack and stack[-1] == "VEVENT" and name not in ev["values"]:
            ev["values"][name], ev["params"][name] = value, params
    return out


def _event(ev: dict[str, Any], method: str, customs: dict[str, _Custom], default_tz: str) -> dict[str, Any] | None:
    v, p = ev["values"], ev["params"]
    start = _when(v.get("DTSTART", ""), p.get("DTSTART", {}), customs, default_tz)
    if start is None:
        return None
    end = _when(v.get("DTEND", ""), p.get("DTEND", {}), customs, default_tz) if v.get("DTEND") else None
    if end is None:
        end = start + (_duration(v.get("DURATION", "")) or timedelta(minutes=30))
    organizer = re.sub(r"(?i)^mailto:", "", v.get("ORGANIZER", "")).strip()
    cancelled = method == "CANCEL" or v.get("STATUS", "").upper() == "CANCELLED"
    return {
        "uid": v.get("UID", "").strip(), "title": _text(v.get("SUMMARY", "")) or "Meeting",
        "start": to_iso(start), "end": to_iso(end), "location": _text(v.get("LOCATION", "")),
        "description": _text(v.get("DESCRIPTION", ""))[:2000], "organizer_email": organizer.lower(),
        "organizer_name": p.get("ORGANIZER", {}).get("CN", ""), "method": "cancel" if cancelled else "request",
        "source": "ics",
    }


def _link_time(value: str, zone: str, default_tz: str) -> datetime | None:
    value = (value or "").strip()
    if re.fullmatch(r"\d{8}|\d{4}-\d{2}-\d{2}", value):
        return None  # all-day: not a meeting
    if re.fullmatch(r"\d{8}T\d{4,6}Z?", value):
        return _when(value, {"TZID": zone} if zone and not value.endswith("Z") else {}, {}, default_tz)
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_zone(zone) or ZoneInfo(default_tz))


def from_link(url: str, default_tz: str = "UTC") -> dict[str, Any] | None:
    """An invite from a Google Calendar, Outlook, or Yahoo "Add to calendar" link, if it carries the time."""
    u = urlparse(unescape(url))
    host, q = u.netloc.lower(), {k: v[0] for k, v in parse_qs(u.query).items()}
    start = end = None
    if host.endswith("google.com") and "calendar" in u.path and q.get("action", "").upper() == "TEMPLATE":
        a, _, b = q.get("dates", "").partition("/")
        start, end = _link_time(a, q.get("ctz", ""), default_tz), _link_time(b, q.get("ctz", ""), default_tz)
        title, details, location = q.get("text", ""), q.get("details", ""), q.get("location", "")
    elif ("outlook.live.com" in host or "outlook.office" in host) and "compose" in u.path:
        start, end = _link_time(q.get("startdt", ""), "", default_tz), _link_time(q.get("enddt", ""), "", default_tz)
        title, details, location = q.get("subject", ""), q.get("body", ""), q.get("location", "")
    elif host == "calendar.yahoo.com":
        start = _link_time(q.get("st", ""), "", default_tz)
        end = _link_time(q.get("et", ""), "", default_tz)
        if start and not end and re.fullmatch(r"\d{4}", q.get("dur", "")):
            end = start + timedelta(hours=int(q["dur"][:2]), minutes=int(q["dur"][2:]))
        title, details, location = q.get("title", ""), q.get("desc", ""), q.get("in_loc", "")
    else:
        return None
    if start is None:
        return None
    return {"uid": "", "title": title or "Meeting", "start": to_iso(start),
            "end": to_iso(end if end and end > start else start + timedelta(minutes=30)), "location": location,
            "description": details[:2000], "organizer_email": "", "organizer_name": "", "method": "request",
            "source": "link"}


ANCHOR = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL)


def links(html: str, text: str, limit: int = 60) -> list[dict[str, str]]:
    """The links in an email with their visible text ("Start Test" -> https://...), for things like test links."""
    out: list[dict[str, str]] = []
    seen = set()
    found = [(unescape(u), re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", t))).strip())
             for u, t in ANCHOR.findall(html or "")] + [(u, "") for u in URL.findall(text or "")]
    for url, label in found:
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append({"url": url[:2000], "text": label[:120]})
    return out[:limit]


def extract(ics_texts: list[str], html: str, text: str, default_tz: str = "UTC") -> dict[str, Any]:
    """Every invite in an email, and whether it offers "Add to Calendar" at all (even when unreadable)."""
    invites = [i for t in ics_texts for i in parse_ics(t, default_tz)]
    urls = HREF.findall(html or "") + URL.findall(text or "")
    if not invites:  # links duplicate the attachment when both are there
        seen = set()
        for url in urls:
            inv = from_link(url, default_tz)
            if inv and (inv["start"], inv["title"]) not in seen:
                seen.add((inv["start"], inv["title"]))
                invites.append(inv)
    offered = bool(invites or ics_texts or ADD_TO_CALENDAR.search(f"{text}\n{html}"))
    return {"invites": invites, "add_to_calendar": offered, "links": links(html, text)}
