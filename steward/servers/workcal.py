"""Work calendar MCP server: your company (Microsoft 365) calendar, read-only.

Two ways in, both ones your company controls:
- ics: the calendar link Outlook gives you when you publish your calendar (Settings > Calendar > Shared
  calendars > Publish a calendar). The link is a secret, kept in .env.
- graph: Microsoft's sign-in with your work account (see steward.microsoft), read-only calendar access.

Only what the chosen detail level allows ever leaves this server: never the meeting body or the attendee list.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from steward import microsoft
from steward.invites import parse_ics
from steward.recurrence import expand
from steward.servers.common import READ_ONLY_REMOTE, guarded, make_server, server_config
from steward.timeutil import parse_iso, to_iso, utcnow

cfg = server_config()
w_cfg = cfg.work_calendar
mcp = make_server("steward-workcal", "Read your work (Microsoft 365) calendar, read-only.")

JOIN = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+|https://[\w.-]*zoom\.us/j/[^\s\"'<>]+")


def _uid(*parts: str) -> str:
    return "work-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:20] + "@jobsourcer"


def shape(raw: dict[str, Any], detail: str, prefix: str) -> dict[str, Any]:
    """What goes on your personal calendar for one work meeting, at the chosen detail level."""
    if detail == "busy":
        return {**raw, "title": "Busy (work)", "location": "", "join_url": "", "organizer": ""}
    if detail == "title":
        return {**raw, "title": f"{prefix}{raw['title']}", "location": "", "join_url": "", "organizer": ""}
    return {**raw, "title": f"{prefix}{raw['title']}"}


def _from_ics(start: datetime, end: datetime) -> list[dict[str, Any]]:
    url = os.environ.get(w_cfg.ics_url_env, "").strip()
    if not url:
        raise RuntimeError(f"Paste your published calendar link in Setup first ({w_cfg.ics_url_env} is empty)")
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    resp = httpx.get(url, timeout=60, follow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"The calendar link answered HTTP {resp.status_code}: was it unpublished or reset?")
    if "BEGIN:VCALENDAR" not in resp.text[:2000]:
        raise RuntimeError("The calendar link did not return a calendar (check you copied the ICS link, not HTML)")
    out = []
    for e in expand(parse_ics(resp.text, cfg.profile.timezone), start, end):
        out.append({"uid": _uid(e["uid"], e.get("occurrence") or e["start"]), "title": e["title"] or "Meeting",
                    "start": e["start"], "end": e["end"], "location": e.get("location", ""),
                    "join_url": (JOIN.search(f"{e.get('location', '')}\n{e.get('description', '')}") or [""])[0],
                    "organizer": e.get("organizer_name") or e.get("organizer_email", ""),
                    "show_as": (e.get("show_as") or "BUSY").lower()})
    return out


def _from_graph(start: datetime, end: datetime) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {microsoft.access_token(cfg)}", "Prefer": 'outlook.timezone="UTC"'}
    url = (f"{microsoft.GRAPH}/me/calendarView?startDateTime={to_iso(start)}&endDateTime={to_iso(end)}"
           "&$top=200&$select=id,iCalUId,subject,start,end,location,isAllDay,isCancelled,showAs,onlineMeeting,"
           "organizer")
    out = []
    with httpx.Client(timeout=60) as c:
        while url:
            resp = c.get(url, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"Microsoft Graph answered HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            for e in data.get("value", []):
                if e.get("isCancelled") or e.get("isAllDay"):
                    continue
                s = parse_iso(e["start"]["dateTime"][:19] + "+00:00")
                f = parse_iso(e["end"]["dateTime"][:19] + "+00:00")
                if not s or not f:
                    continue
                out.append({"uid": _uid(e["id"]), "title": e.get("subject") or "Meeting", "start": to_iso(s),
                            "end": to_iso(f), "location": (e.get("location") or {}).get("displayName", ""),
                            "join_url": (e.get("onlineMeeting") or {}).get("joinUrl", ""),
                            "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("name", ""),
                            "show_as": (e.get("showAs") or "busy").lower()})
            url = data.get("@odata.nextLink", "")
    return out


def read(days: int) -> list[dict[str, Any]]:
    start = utcnow() - timedelta(hours=1)
    end = start + timedelta(days=days)
    raw = _from_graph(start, end) if w_cfg.source == "graph" else _from_ics(start, end)
    keep = [e for e in raw if w_cfg.include_free or e["show_as"] not in ("free", "workingelsewhere")]
    return sorted((shape(e, w_cfg.detail, w_cfg.title_prefix) for e in keep), key=lambda e: e["start"])


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def events(days: int = 30) -> dict[str, Any]:
    """Your work meetings from now on (single meetings: repeats expanded), at the configured detail level."""
    return {"source": w_cfg.source, "detail": w_cfg.detail, "events": read(days)}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def status() -> dict[str, Any]:
    """Check the work calendar can be read: how many meetings it has in the next 7 days."""
    week = read(7)
    who = microsoft.account(cfg) if w_cfg.source == "graph" else ""
    return {"ok": True, "source": w_cfg.source, "account": who, "next_7_days": len(week),
            "next": week[0]["start"] if week else ""}


if __name__ == "__main__":
    mcp.run()
