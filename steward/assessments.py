"""Online assessments: never miss one.

An email inviting you to an online assessment (HackerRank, Codility, CodeSignal,
TestGorilla, ...) becomes an open task with the test link and the deadline, so
the deadline shows under "Upcoming meetings & deadlines" and gets reminders. A
reminder for the same test updates that task instead of adding another.

This does not depend on the AI noticing the assessment: the platform, the link
(read from the email's HTML, where "Start Test" buttons live), and deadlines such
as "End Login Date/Time: 23 Oct 2026 01:50 PM -03" are read directly.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from steward import invites
from steward.timeutil import parse_iso, to_iso

PLATFORMS = {
    "hackerrank": "HackerRank", "codility": "Codility", "codesignal": "CodeSignal", "testgorilla": "TestGorilla",
    "hackerearth": "HackerEarth", "coderbyte": "Coderbyte", "karat": "Karat", "mettl": "Mercer Mettl",
    "imocha": "iMocha", "testdome": "TestDome", "devskiller": "DevSkiller", "coderpad": "CoderPad",
    "qualified.io": "Qualified", "woven": "Woven", "vervoe": "Vervoe", "canditech": "Canditech",
    "leetcode": "LeetCode", "codingame": "CodinGame", "byteboard": "Byteboard", "adaface": "Adaface",
    "glider": "Glider AI", "hirevue": "HireVue", "criteria corp": "Criteria", "shl": "SHL",
}
PLATFORM_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in PLATFORMS) + r")\b", re.IGNORECASE)
ASSESSMENT_RE = re.compile(
    r"\b(online |technical |coding |skills? |programming )?(assessment|test|challenge|evaluation|exercise|quiz)\b",
    re.IGNORECASE)
INVITE_RE = re.compile(r"invit|take (the|this|a|an|your)|complete (the|this|your)|start (the |your )?(test|assessment|"
                       r"challenge)|you have been|you've been|reminder|expires?|deadline|before|by ", re.IGNORECASE)
LINK_TEXT_RE = re.compile(r"start|begin|take|launch|open|access|go to|test link|assessment link|challenge link",
                          re.IGNORECASE)
LINK_URL_RE = re.compile(r"test|assess|challenge|invite|candidate|exam|interview", re.IGNORECASE)
NOT_A_TEST_LINK = re.compile(r"unsubscribe|support|faq|privacy|terms|help|preferences|candidatesupport|"
                             r"customersupport|policy|/hc/", re.IGNORECASE)
DEADLINE_KEY = re.compile(
    r"end login date/?time|expir(?:es|y|ation)(?: date)?(?: on)?|deadline|due (?:by|on|date)?|"
    r"(?:complete|finish|submit|take)[^.\n]{0,40}?\b(?:by|before|until)|no later than|available until|valid until|"
    r"closes? (?:on|at)?|link (?:is )?(?:valid|active) until",
    re.IGNORECASE)

ABBREVIATIONS = {"UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
                 "PST": -8, "PDT": -7, "BST": 1, "CET": 1, "CEST": 2, "IST": 5.5, "ART": -3, "BRT": -3}
_TIME = r"(?:,?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*([AaPp]\.?[Mm]\.?)?)?"
_ZONE = r"\s*\(?\s*(UTC|GMT|Z|[A-Z]{3,4}|[+-]\d{2}(?::?\d{2})?)?\)?"
DATE_PATTERNS = [
    # 23 Oct 2026 01:50 PM -03
    ("dmy", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})" + _TIME + _ZONE)),
    # October 23, 2026 at 11:59 PM (UTC)
    ("mdy", re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})" + _TIME + _ZONE)),
    # 2026-10-23 13:50 +02:00
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?" + r"\s*(Z|UTC|[+-]\d{2}:?\d{2})?")),
    # Monday, October 26 (no year: the next one)
    ("md", re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b" + _TIME + _ZONE)),
    # 26 October (no year)
    ("dm", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\b" + _TIME + _ZONE)),
]


def from_platform(address: str) -> bool:
    """Mail sent by the assessment platform itself (support@hackerrankforwork.com, no-reply@codility.com, ...)."""
    domain = (address or "").rsplit("@", 1)[-1].lower()
    return any(key.split(".")[0].replace(" ", "") in domain for key in PLATFORMS) if domain else False


def _zone(token: str | None, default_tz: str):
    if not token:
        return ZoneInfo(default_tz)
    token = token.strip()
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", token)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(-delta if m.group(1) == "-" else delta)
    if token.upper() in ABBREVIATIONS:
        return timezone(timedelta(hours=ABBREVIATIONS[token.upper()]))
    return ZoneInfo(default_tz)


def _clock(hour: str | None, minute: str | None, ampm: str | None) -> tuple[int, int] | None:
    if hour is None:
        return None
    h, mi = int(hour), int(minute or 0)
    marker = (ampm or "").lower().replace(".", "")
    if marker == "pm" and h < 12:
        h += 12
    elif marker == "am" and h == 12:
        h = 0
    return (h, mi) if h < 24 and mi < 60 else None


MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december")


def _month(word: str) -> int | None:
    """'Oct', 'October', 'Sept' -> 10, 10, 9; 'Monday', 'Duration' -> None."""
    w = word.lower().rstrip(".")
    return next((i + 1 for i, name in enumerate(MONTH_NAMES) if len(w) >= 3 and name.startswith(w)),
                9 if w == "sept" else None)


def parse_date(text: str, default_tz: str = "UTC", ref: datetime | None = None) -> datetime | None:
    """The first date (and time, and zone) in `text`. A date without a time means 23:59 that day; one without a
    year means the next such date after `ref` (the email's date)."""
    for kind, pattern in DATE_PATTERNS:
        for m in pattern.finditer(text):
            g = m.groups()
            try:
                if kind == "iso":
                    year, month, day = int(g[0]), int(g[1]), int(g[2])
                    clock, zone = ((int(g[3]), int(g[4])) if g[3] else None), g[5]
                else:
                    month_word, day = (g[1], int(g[0])) if kind in ("dmy", "dm") else (g[0], int(g[1]))
                    month = _month(month_word)
                    rest = g[3:] if kind in ("dmy", "mdy") else g[2:]
                    year = int(g[2]) if kind in ("dmy", "mdy") else None
                    clock, zone = _clock(rest[0], rest[1], rest[2]), rest[3]
                if not month:
                    continue
                h, mi = clock or (23, 59)
                zone_info = _zone(zone, default_tz)
                if year is None:
                    base = (ref or datetime.now(timezone.utc)).astimezone(zone_info)
                    year = base.year
                    if datetime(year, month, day, h, mi, tzinfo=zone_info) < base - timedelta(days=1):
                        year += 1
                return datetime(year, month, day, h, mi, tzinfo=zone_info)
            except ValueError:
                continue
    return None


def deadline(text: str, default_tz: str = "UTC", received: str | None = None) -> datetime | None:
    """The deadline next to words like "expires", "deadline", "complete it by", "End Login Date/Time"."""
    base = parse_iso(received) if received else None
    for key in DEADLINE_KEY.finditer(text):
        found = parse_date(text[key.end():key.end() + 120], default_tz, base)
        if found:
            return found
    m = re.search(r"(?:expires?|within|in)\s+(\d{1,2})\s+(day|hour)s?", text, re.IGNORECASE)
    if m and base:
        delta = timedelta(days=int(m.group(1))) if m.group(2).lower() == "day" else timedelta(hours=int(m.group(1)))
        return base + delta
    return None


def _test_link(links: list[dict[str, str]], platform_key: str) -> str:
    best, score = "", 0
    for link in links:
        url, text = link.get("url", ""), link.get("text", "")
        if not url.startswith("http") or NOT_A_TEST_LINK.search(url) or NOT_A_TEST_LINK.search(text):
            continue
        s = (3 if LINK_TEXT_RE.search(text) else 0) + (2 if platform_key and platform_key in url.lower() else 0) + \
            (1 if LINK_URL_RE.search(url) else 0)
        if s > score:
            best, score = url, s
    return best if score >= 2 else ""


def detect(msg: dict[str, Any], default_tz: str = "UTC", company: str = "") -> dict[str, Any] | None:
    """An assessment invitation (or its reminder) in an email, or None."""
    subject, body = msg.get("subject", "") or "", msg.get("body_text", "") or ""
    text = f"{subject}\n{body}"
    links = msg.get("links") or invites.links("", body)
    where = PLATFORM_RE.search(f"{text}\n{msg.get('from_email', '')}\n" + " ".join(l.get("url", "") for l in links))
    platform_key = where.group(1).lower() if where else ""
    named = ASSESSMENT_RE.search(text)
    if not (platform_key or (named and INVITE_RE.search(text))):
        return None
    if not named and not re.search(r"invit|test|assess|challenge", text, re.IGNORECASE):
        return None  # a platform newsletter, not a test
    platform = PLATFORMS.get(platform_key, "")
    due = deadline(body, default_tz, msg.get("date") or msg.get("received_at"))
    # "[2026] Cloud Engineer (Azure) - powered by HackerRank" / "Your HackerRank X Invitation" -> "X"
    title = re.sub(r"(?i)^(re|fwd?):\s*", "", subject).strip()
    title = re.sub(r"(?i)^your\s+(invite to\s+)?", "", title)
    title = re.sub(r"(?i)\s+(invitation|invite|expires in \d+ \w+|reminder)$", "", title)
    title = re.sub(re.escape(platform), "", title, flags=re.IGNORECASE).strip(" -:") if platform else title
    who = company or (re.search(r"(?:position|role|opportunity|job) at ([A-Z][\w&.\- ]{1,40}?)[.,\n]", body) or [None, ""])[1]
    m = re.search(r"Duration:?\s*(\d+\s*(?:min(?:ute)?s?|hours?|h))", body, re.IGNORECASE)
    return {
        "platform": platform, "title": title or "Online assessment", "company": (who or "").strip(),
        "link": _test_link(links, platform_key.split(".")[0] if platform_key else ""),
        "due_at": to_iso(due) if due else None, "duration": m.group(1) if m else "",
    }


def task_title(found: dict[str, Any]) -> str:
    parts = ["Take the"]
    if found["company"]:
        parts.append(found["company"])
    parts.append(found["title"])
    parts.append("assessment" if "assessment" not in found["title"].lower() else "")
    if found["platform"]:
        parts.append(f"({found['platform']})")
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
