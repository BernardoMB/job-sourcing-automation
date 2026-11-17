"""Your work (Microsoft 365) calendar mirrored onto your personal calendar."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from steward import activity, microsoft
from steward.invites import parse_ics
from steward.mcp_hub import MCPHub
from steward.recurrence import expand
from steward.servers.workcal import shape
from steward.timeutil import parse_iso, to_iso, utcnow

from .conftest import run, sandbox_json

OUTLOOK = """BEGIN:VCALENDAR
METHOD:PUBLISH
BEGIN:VTIMEZONE
TZID:Central Standard Time (Mexico)
END:VTIMEZONE
BEGIN:VEVENT
UID:standup-1
SUMMARY:Daily standup
DTSTART;TZID=Central Standard Time (Mexico):20261102T093000
DTEND;TZID=Central Standard Time (Mexico):20261102T094500
RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE,FR;UNTIL=20261231T235959Z
EXDATE;TZID=Central Standard Time (Mexico):20261111T093000
X-MICROSOFT-CDO-BUSYSTATUS:BUSY
END:VEVENT
BEGIN:VEVENT
UID:standup-1
RECURRENCE-ID;TZID=Central Standard Time (Mexico):20261113T093000
SUMMARY:Daily standup (moved)
DTSTART;TZID=Central Standard Time (Mexico):20261113T110000
DTEND;TZID=Central Standard Time (Mexico):20261113T111500
END:VEVENT
BEGIN:VEVENT
UID:review-1
SUMMARY:Monthly review
DTSTART;TZID=Eastern Standard Time:20261030T140000
DTEND;TZID=Eastern Standard Time:20261030T150000
RRULE:FREQ=MONTHLY;BYDAY=-1FR;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:once-1
SUMMARY:Offsite
DTSTART:20261118T160000Z
DTEND:20261118T170000Z
END:VEVENT
END:VCALENDAR
"""


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_repeating_work_meetings_become_single_meetings():
    events = expand(parse_ics(OUTLOOK), _utc("2026-11-09T00:00:00"), _utc("2026-12-31T00:00:00"))
    standups = sorted(parse_iso(e["start"]) for e in events if e["uid"] == "standup-1")
    nov = [s for s in standups if s < _utc("2026-11-16T00:00:00")]
    # Mon 9, Wed 11 cancelled (EXDATE), Fri 13 moved to 11:00. Mexico City is UTC-6.
    assert nov == [_utc("2026-11-09T15:30:00"), _utc("2026-11-13T17:00:00")]
    assert next(e for e in events if e["start"] == to_iso(_utc("2026-11-13T17:00:00")))["title"] == "Daily standup (moved)"
    # Last Friday of the month, 3 times from October: Nov 27 and Dec 25 fall in the window, at 14:00 New York time.
    reviews = sorted(parse_iso(e["start"]) for e in events if e["uid"] == "review-1")
    assert reviews == [_utc("2026-11-27T19:00:00"), _utc("2026-12-25T19:00:00")]
    assert [e["title"] for e in events if e["uid"] == "once-1"] == ["Offsite"]


def test_detail_levels():
    raw = {"uid": "u", "title": "Q4 budget", "start": "", "end": "", "location": "Room 4",
           "join_url": "https://teams.microsoft.com/l/meetup-join/x", "organizer": "Ana", "show_as": "busy"}
    assert shape(raw, "busy", "[Work] ")["title"] == "Busy (work)"
    assert shape(raw, "busy", "[Work] ")["join_url"] == ""
    titled = shape(raw, "title", "[Work] ")
    assert titled["title"] == "[Work] Q4 budget" and not titled["location"] and not titled["organizer"]
    assert shape(raw, "full", "")["join_url"].startswith("https://teams")


def _work_events(monkeypatch, rows: list[dict]):
    real = MCPHub.call

    async def call(self, server, tool, args=None, *a, **kw):
        if server == "workcal":
            return {"source": "ics", "detail": "title", "events": list(rows)}
        return await real(self, server, tool, args, *a, **kw)

    monkeypatch.setattr(MCPHub, "call", call)


def test_work_calendar_is_mirrored_every_check(steward, monkeypatch):
    steward.cfg.work_calendar.enabled = True
    start = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    rows = [{"uid": f"work-{i}@jobsourcer", "title": f"[Work] Meeting {i}", "start": to_iso(start + timedelta(hours=i)),
             "end": to_iso(start + timedelta(hours=i, minutes=30)), "location": "", "join_url": "", "organizer": "",
             "show_as": "busy"} for i in range(5)]
    _work_events(monkeypatch, rows)

    async def sync():
        async with MCPHub(steward.cfg) as hub:
            return await steward.sync_work_calendar(hub)

    def copies():
        return {e["ical_uid"]: e for e in sandbox_json(steward, "calendar.json")["events"]
                if (e.get("ical_uid") or "").startswith("work-")}

    assert run(sync()) == 5
    assert len(copies()) == 5 and copies()["work-0@jobsourcer"]["attendees"] == []
    assert run(sync()) == 0  # nothing changed: nothing written
    # One meeting moved, one cancelled.
    rows[1] = {**rows[1], "start": to_iso(start + timedelta(days=1)), "end": to_iso(start + timedelta(days=1, minutes=30))}
    cancelled = rows.pop(4)
    assert run(sync()) == 2
    now = copies()
    assert parse_iso(now["work-1@jobsourcer"]["start"]) == start + timedelta(days=1)
    assert cancelled["uid"] not in now and len(now) == 4
    assert any("Work calendar: 0 meeting(s) added, 1 moved, 1 cancelled" in a["message"]
               for a in activity.recent(steward.db))
    # A broken link (empty calendar) removes nothing.
    rows.clear()
    assert run(sync()) == 0 and len(copies()) == 4
    # Work meetings now block those times when Job Sourcer offers availability.
    async def busy():
        async with MCPHub(steward.cfg) as hub:
            return await hub.call("calendar", "check_availability", {"start": to_iso(start), "end": to_iso(start + timedelta(minutes=30))})
    assert run(busy())["free"] is False


def test_microsoft_sign_in_saves_and_refreshes_the_token(workspace, monkeypatch):
    from steward.config import load_config
    cfg = load_config(workspace)
    cfg.work_calendar.graph_client_id = "client-123"
    answers = iter([{"error": "authorization_pending"},
                    {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}])
    posts = []

    def post(url, data=None, timeout=None):
        posts.append((url, data))
        if url.endswith("/devicecode"):
            return httpx.Response(200, json={"user_code": "ABC-123", "verification_uri": "https://microsoft.com/devicelogin",
                                             "device_code": "dc", "interval": 1, "expires_in": 900})
        if data.get("grant_type") == "refresh_token":
            return httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 3600})
        body = next(answers)
        return httpx.Response(400 if "error" in body else 200, json=body)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None:
                        httpx.Response(200, json={"userPrincipalName": "me@company.example"}))
    flow = microsoft.start_sign_in(cfg)
    assert flow["user_code"] == "ABC-123" and "Calendars.Read" in posts[0][1]["scope"]
    assert microsoft.finish_sign_in(cfg, flow, sleep=lambda s: None) == {"account": "me@company.example"}
    assert microsoft.access_token(cfg) == "a1"
    path = cfg.resolve(cfg.work_calendar.token_file)
    token = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**token, "expires_at": time.time() - 1}), encoding="utf-8")
    assert microsoft.access_token(cfg) == "a2"  # expired: refreshed with the saved refresh token
    assert microsoft.account(cfg) == "me@company.example"
