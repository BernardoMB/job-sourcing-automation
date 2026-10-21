"""Calendar MCP server: availability, conflict detection, slot finding, booking, RSVP.

Backends: Google Calendar (free API, OAuth on your own Google Cloud project)
or a local sandbox calendar. Availability checks and writes are separate calls,
so the executor re-checks availability immediately before every booking.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from steward.scheduling import conflicts, evaluate_slot, find_slots
from steward.servers.common import READ_ONLY_REMOTE, WRITE, JsonStore, guarded, make_server, server_config
from steward.timeutil import parse_iso, to_iso, utcnow

cfg = server_config()
cal_cfg = cfg.calendar
mcp = make_server(
    "steward-calendar",
    "Check availability, detect conflicts, find free slots, and (executor only) book meetings or RSVP.",
)
SCOPES = ["https://www.googleapis.com/auth/calendar"]


class SandboxCalendar:
    def __init__(self) -> None:
        self.store = JsonStore(cfg.sandbox_path / "calendar.json", {"events": []})

    def list(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        out = []
        for e in self.store.load()["events"]:
            s, f = parse_iso(e["start"]), parse_iso(e["end"])
            if s and f and s < end and start < f and e.get("status", "confirmed") != "cancelled":
                out.append(e)
        return sorted(out, key=lambda e: e["start"])

    def busy(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return [
            {"id": e["id"], "title": e.get("title", ""), "start": e["start"], "end": e["end"]}
            for e in self.list(start, end)
            if not e.get("transparent") and e.get("my_response") != "declined"
        ]

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self.store.load()
        event_id = f"stw-{uuid.uuid4().hex[:10]}"
        link = f"https://meet.example.com/{event_id}" if body.get("add_video_link") else ""
        event = {
            "id": event_id,
            "title": body["title"],
            "start": body["start"],
            "end": body["end"],
            "description": body.get("description", ""),
            "location": body.get("location", ""),
            "meeting_link": link,
            "attendees": [{"email": a, "name": "", "response": "needsAction"} for a in body.get("attendees", [])],
            "organizer": {"email": cfg.profile.email or "me@example.com", "name": cfg.profile.name},
            "my_response": "accepted",
            "created_by_steward": True,
        }
        data["events"].append(event)
        self.store.save(data)
        return {"id": event_id, "meeting_link": link, "html_link": ""}

    def rsvp(self, event_id: str, response: str) -> dict[str, Any]:
        data = self.store.load()
        for e in data["events"]:
            if e["id"] == event_id:
                e["my_response"] = response
                self.store.save(data)
                return {"status": "ok", "event_id": event_id, "response": response}
        raise ValueError(f"No event {event_id}")


class GoogleCalendar:
    def __init__(self) -> None:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install the Google extra: pip install 'steward[google]'") from exc
        token_path = cfg.resolve(cal_cfg.token_file)
        if not token_path.exists():
            raise RuntimeError("No Google token yet. Run `steward google-auth` once.")
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.cal = cal_cfg.calendar_id

    @staticmethod
    def _norm(item: dict[str, Any]) -> dict[str, Any]:
        start = item.get("start", {})
        end = item.get("end", {})
        link = item.get("hangoutLink", "")
        for ep in (item.get("conferenceData", {}) or {}).get("entryPoints", []) or []:
            if ep.get("entryPointType") == "video":
                link = ep.get("uri", link)
        attendees = item.get("attendees", []) or []
        me = next((a for a in attendees if a.get("self")), None)
        priv = ((item.get("extendedProperties") or {}).get("private") or {})
        return {
            "id": item["id"],
            "title": item.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "all_day": "date" in start,
            "location": item.get("location", ""),
            "description": (item.get("description") or "")[:2000],
            "meeting_link": link,
            "attendees": [
                {"email": a.get("email", ""), "name": a.get("displayName", ""), "response": a.get("responseStatus", "")}
                for a in attendees
            ],
            "organizer": {"email": (item.get("organizer") or {}).get("email", ""),
                          "name": (item.get("organizer") or {}).get("displayName", "")},
            "my_response": me.get("responseStatus", "accepted") if me else "accepted",
            "transparent": item.get("transparency") == "transparent",
            "created_by_steward": priv.get("steward") == "1",
            "status": item.get("status", "confirmed"),
        }

    def list(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        res = self.svc.events().list(
            calendarId=self.cal, timeMin=to_iso(start), timeMax=to_iso(end), singleEvents=True,
            orderBy="startTime", maxResults=250,
        ).execute()
        return [self._norm(i) for i in res.get("items", []) if i.get("status") != "cancelled"]

    def busy(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        res = self.svc.freebusy().query(
            body={"timeMin": to_iso(start), "timeMax": to_iso(end), "items": [{"id": self.cal}]}
        ).execute()
        busy = res.get("calendars", {}).get(self.cal, {}).get("busy", [])
        titled = {(e["start"], e["end"]): e for e in self.list(start, end)}
        out = []
        for b in busy:
            match = next((e for (s, f), e in titled.items()
                          if parse_iso(s) == parse_iso(b["start"]) and parse_iso(f) == parse_iso(b["end"])), None)
            out.append({"id": match["id"] if match else "", "title": match["title"] if match else "Busy",
                        "start": b["start"], "end": b["end"]})
        return out

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        event = {
            "summary": body["title"],
            "description": body.get("description", ""),
            "location": body.get("location", ""),
            "start": {"dateTime": body["start"]},
            "end": {"dateTime": body["end"]},
            "attendees": [{"email": a} for a in body.get("attendees", [])],
            "extendedProperties": {"private": {"steward": "1"}},
        }
        if body.get("add_video_link"):
            event["conferenceData"] = {
                "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
            }
        res = self.svc.events().insert(
            calendarId=self.cal, body=event, sendUpdates="all" if body.get("send_updates", True) else "none",
            conferenceDataVersion=1,
        ).execute()
        norm = self._norm(res)
        return {"id": res["id"], "meeting_link": norm["meeting_link"], "html_link": res.get("htmlLink", "")}

    def rsvp(self, event_id: str, response: str) -> dict[str, Any]:
        item = self.svc.events().get(calendarId=self.cal, eventId=event_id).execute()
        attendees = item.get("attendees", []) or []
        found = False
        for a in attendees:
            if a.get("self"):
                a["responseStatus"] = response
                found = True
        if not found:
            raise ValueError("You are not an attendee of this event")
        self.svc.events().patch(calendarId=self.cal, eventId=event_id, body={"attendees": attendees},
                                sendUpdates="all").execute()
        return {"status": "ok", "event_id": event_id, "response": response}


_backend: SandboxCalendar | GoogleCalendar | None = None


def backend() -> SandboxCalendar | GoogleCalendar:
    global _backend
    if _backend is None:
        if cal_cfg.backend == "google":
            _backend = GoogleCalendar()
        elif cal_cfg.backend == "sandbox":
            _backend = SandboxCalendar()
        else:
            raise RuntimeError("Calendar integration is disabled in steward.yaml")
    return _backend


def _dt(value: str, name: str) -> datetime:
    d = parse_iso(value, cfg.profile.timezone)
    if d is None:
        raise ValueError(f"Invalid ISO datetime for {name}: {value!r}")
    return d


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def list_events(start: str, end: str) -> dict[str, Any]:
    """Events between two ISO datetimes, with meeting links and attendees."""
    return {"events": backend().list(_dt(start, "start"), _dt(end, "end"))}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def check_availability(start: str, end: str, buffer_minutes: int = 0) -> dict[str, Any]:
    """Whether [start, end] is free, honoring a buffer before and after. Lists any conflicts."""
    s, e = _dt(start, "start"), _dt(end, "end")
    pad = timedelta(minutes=buffer_minutes)
    busy = backend().busy(s - pad - timedelta(hours=1), e + pad + timedelta(hours=1))
    hits = conflicts(busy, s, e, buffer_minutes)
    sch = cfg.scheduling
    full = evaluate_slot(busy, s, e, tz_name=cfg.profile.timezone, working_days=sch.working_days,
                         work_start=sch.work_start, work_end=sch.work_end, buffer_minutes=buffer_minutes,
                         min_notice_hours=0, avoid=sch.avoid, now=utcnow())
    return {"free": not hits, "conflicts": hits, "problems": full["problems"], "start": to_iso(s), "end": to_iso(e)}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def find_free_slots(
    window_start: str,
    window_end: str,
    duration_minutes: int = 30,
    max_slots: int = 3,
    buffer_minutes: int = -1,
    min_notice_hours: int = -1,
) -> dict[str, Any]:
    """Free slots inside working hours, honoring buffers, minimum notice, and blocked windows.

    Negative buffer/notice values mean "use the configured default".
    """
    sch = cfg.scheduling
    ws, we = _dt(window_start, "window_start"), _dt(window_end, "window_end")
    busy = backend().busy(ws - timedelta(hours=2), we + timedelta(hours=2))
    slots = find_slots(
        busy, ws, we,
        duration_minutes=duration_minutes, tz_name=cfg.profile.timezone, working_days=sch.working_days,
        work_start=sch.work_start, work_end=sch.work_end,
        buffer_minutes=sch.buffer_minutes if buffer_minutes < 0 else buffer_minutes,
        min_notice_hours=sch.min_notice_hours if min_notice_hours < 0 else min_notice_hours,
        avoid=sch.avoid, max_slots=max_slots, now=utcnow(),
    )
    return {"slots": slots, "timezone": cfg.profile.timezone}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
def pending_invites(days: int = 21) -> dict[str, Any]:
    """Upcoming invitations from other people that you have not answered yet."""
    now = utcnow()
    events = backend().list(now, now + timedelta(days=days))
    me = (cfg.profile.email or "").lower()
    invites = [e for e in events if e.get("my_response") == "needsAction"
               and (e.get("organizer", {}).get("email", "").lower() != me)]
    return {"invites": invites}


@mcp.tool(annotations=WRITE)
@guarded
def create_event(
    title: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    description: str = "",
    location: str = "",
    add_video_link: bool = True,
    send_updates: bool = True,
) -> dict[str, Any]:
    """Book a meeting and invite attendees. Executor only; re-check availability first."""
    s, e = _dt(start, "start"), _dt(end, "end")
    if e <= s:
        raise ValueError("end must be after start")
    return backend().create(
        {
            "title": title, "start": to_iso(s), "end": to_iso(e), "attendees": attendees or [],
            "description": description, "location": location, "add_video_link": add_video_link,
            "send_updates": send_updates,
        }
    )


@mcp.tool(annotations=WRITE)
@guarded
def rsvp_event(event_id: str, response: str = "accepted") -> dict[str, Any]:
    """Answer an invitation: accepted, declined, or tentative. Executor only."""
    if response not in ("accepted", "declined", "tentative"):
        raise ValueError("response must be accepted, declined, or tentative")
    return backend().rsvp(event_id, response)


if __name__ == "__main__":
    mcp.run()
