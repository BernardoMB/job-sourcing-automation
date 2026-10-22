"""Reply channels, LinkedIn notification emails, recruiter invites, the Logs and Queue tabs, and Execute."""

from __future__ import annotations

import json
from datetime import timedelta

import yaml
from fastapi.testclient import TestClient

from steward import activity, invites
from steward.config import load_config
from steward.db import dumps, loads
from steward.executor import Executor
from steward.mcp_hub import MCPHub
from steward.timeutil import now_iso, parse_iso, to_iso, tz, utcnow
from steward.web.app import create_app

from .conftest import run, sandbox_json


def _deliver(steward, uid: int, sender: str, subject: str, body: str, **extra) -> None:
    box = sandbox_json(steward, "mailbox.json")
    box["messages"].append({"uid": uid, "message_id": f"<m{uid}@example.test>", "from_name": sender.split("@")[0],
                            "from_email": sender, "to": [steward.cfg.profile.email], "cc": [],
                            "date": now_iso(), "in_reply_to": "", "references": [], "subject": subject,
                            "body_text": body, **extra})
    (steward.cfg.sandbox_path / "mailbox.json").write_text(json.dumps(box), encoding="utf-8")


def _emails_linkedin(draft) -> bool:
    actions = loads(draft["final_actions_json"], None) or loads(draft["actions_json"], [])
    return any(a["type"] == "send_email" and any("linkedin.com" in t for t in a["payload"]["to"]) for a in actions)


# -- channels ----------------------------------------------------------------------
def test_linkedin_emails_are_ignored_and_linkedin_is_checked_right_away(steward):
    run(steward.cycle(force=True))
    steward.db.kv_set("last_poll:linkedin", now_iso())  # LinkedIn is not due for another 10 minutes...
    steward.db.kv_set("last_poll:email", None)
    _deliver(steward, 400, "messages-noreply@linkedin.com", "Maria sent you a new message",
             "Maria Lopez: Hi! Are you open to a Senior Engineer role? Reply on LinkedIn.")
    _deliver(steward, 401, "jobs-noreply@linkedin.com", "30 new jobs for Senior Engineer", "Recruiters are hiring.")
    summary = run(steward.cycle())
    assert "linkedin" in summary["ingested"]  # ...but the notification made it due now
    rows = steward.db.query("SELECT * FROM events WHERE sender_address LIKE '%linkedin.com' ORDER BY id")
    assert len(rows) == 2 and all(r["status"] == "ignored" for r in rows)
    assert "answered on LinkedIn" in rows[0]["error"] and "never answered by email" in rows[1]["error"]
    assert not any(_emails_linkedin(d) for d in steward.db.query("SELECT * FROM drafts"))
    assert steward.db.kv_get("linkedin:check_now") is None
    log = [e["message"] for e in activity.recent(steward.db)]
    assert any(m.startswith("Check (scheduled") and "a LinkedIn email said someone messaged you" in m for m in log)
    # LinkedIn messages get LinkedIn replies, email gets email.
    for d in steward.db.query("SELECT d.*, e.source FROM drafts d JOIN events e ON e.id = d.event_id "
                              "WHERE e.source IN ('email', 'linkedin')"):
        types = {a["type"] for a in loads(d["actions_json"], [])} & {"send_email", "send_linkedin"}
        assert types <= ({"send_linkedin"} if d["source"] == "linkedin" else {"send_email"}), (d["summary"], types)


def test_job_sourcer_never_emails_linkedin(steward):
    run(steward.cycle(force=True))
    ev = steward.db.query_one("SELECT * FROM events WHERE source = 'email' LIMIT 1")
    actions = [{"type": "send_email", "payload": {"to": ["hit-reply@linkedin.com"], "cc": [], "subject": "Re: hi",
                                                  "body": "Thanks!", "in_reply_to": "", "references": []}}]
    base = {"event_id": ev["id"], "channel": "email", "summary": "Reply to an InMail notification",
            "actions_json": dumps(actions), "route": "ask", "created_at": now_iso()}
    queued = steward.db.insert("drafts", {**base, "status": "queued"})

    async def execute():
        async with MCPHub(steward.cfg) as hub:
            return await Executor(steward.cfg, steward.db, steward.ledger, hub).execute_draft(queued)

    res = run(execute())
    assert res["status"] == "failed" and "never emails LinkedIn" in res["reason"]
    assert sandbox_json(steward, "outbox.json")["sent"] == []
    # A draft like that made before the rule existed is withdrawn on the next check.
    pending = steward.db.insert("drafts", {**base, "status": "pending"})
    run(steward.cycle())
    assert steward.db.get_draft(pending)["status"] == "superseded"


# -- invitations ---------------------------------------------------------------------
OUTLOOK_ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
PRODID:Microsoft Exchange Server 2010
BEGIN:VTIMEZONE
TZID:Pacific Standard Time
BEGIN:STANDARD
TZOFFSETTO:-0800
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
BEGIN:DAYLIGHT
TZOFFSETTO:-0700
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
ORGANIZER;CN="Lee, Dana":mailto:dana.lee@acme-cloud.example
UID:040000008200E00074C5B7101A82E008-acme-1
SUMMARY;LANGUAGE=en-US:Technical interview - Senior Platform Engineer
DTSTART;TZID=Pacific Standard Time:{start}
DTEND;TZID=Pacific Standard Time:{end}
LOCATION:Microsoft Teams Meeting
DESCRIPTION:Join on Teams\\, see you then.\\nDana
BEGIN:VALARM
TRIGGER:-PT15M
END:VALARM
END:VEVENT
END:VCALENDAR
"""


def _outlook_invite(days: int = 3) -> tuple[str, str]:
    """An Outlook invitation at 10:00-10:45 Pacific, `days` ahead, and that start time in UTC."""
    local = (utcnow().astimezone(tz("America/Los_Angeles")) + timedelta(days=days)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    fmt = "%Y%m%dT%H%M%S"
    ics = OUTLOOK_ICS.replace("{start}", local.strftime(fmt)).replace("{end}", (local + timedelta(minutes=45)).strftime(fmt))
    return ics, to_iso(local)


def test_reading_invitations():
    ics, start_utc = _outlook_invite()
    [inv] = invites.parse_ics(ics)
    assert inv["start"] == start_utc and parse_iso(inv["end"]) - parse_iso(inv["start"]) == timedelta(minutes=45)
    assert inv["title"] == "Technical interview - Senior Platform Engineer" and inv["method"] == "request"
    assert inv["organizer_email"] == "dana.lee@acme-cloud.example" and inv["organizer_name"] == "Lee, Dana"
    assert inv["description"] == "Join on Teams, see you then.\nDana"
    # A zone only the file itself defines still lands at the right hour.
    custom = ics.replace("Pacific Standard Time", "Customized Time Zone")
    assert invites.parse_ics(custom)[0]["start"] == start_utc
    # UTC, IANA zones, DURATION, cancellations; all-day events are not meetings.
    utc = "BEGIN:VCALENDAR\nMETHOD:CANCEL\nBEGIN:VEVENT\nUID:x\nSUMMARY:Call\nDTSTART:20261028T150000Z\nDURATION:PT1H\nEND:VEVENT\nEND:VCALENDAR"
    [c] = invites.parse_ics(utc)
    assert (c["start"], c["end"], c["method"]) == ("2026-10-28T15:00:00+00:00", "2026-10-28T16:00:00+00:00", "cancel")
    ny = utc.replace("DTSTART:20261028T150000Z", "DTSTART;TZID=America/New_York:20261028T110000")
    assert invites.parse_ics(ny)[0]["start"] == "2026-10-28T15:00:00+00:00"
    assert invites.parse_ics(utc.replace("DTSTART:20261028T150000Z", "DTSTART;VALUE=DATE:20261028")) == []
    # "Add to calendar" links.
    google = ("https://calendar.google.com/calendar/render?action=TEMPLATE&amp;text=Intro%20call%20with%20Acme"
              "&amp;dates=20261028T150000Z/20261028T153000Z&amp;details=Zoom&amp;location=https://zoom.us/j/1")
    found = invites.extract([], f'<a href="{google}">Add to Google Calendar</a>', "Add to calendar")
    assert found["add_to_calendar"] and found["invites"][0]["title"] == "Intro call with Acme"
    assert found["invites"][0]["end"] == "2026-10-28T15:30:00+00:00"
    outlook = "https://outlook.live.com/calendar/0/deeplink/compose?subject=Screen&startdt=2026-10-28T15:00:00Z&enddt=2026-10-28T15:45:00Z"
    assert invites.from_link(outlook)["end"] == "2026-10-28T15:45:00+00:00"
    yahoo = "https://calendar.yahoo.com/?v=60&title=Chat&st=20261028T150000Z&dur=0100"
    assert invites.from_link(yahoo)["end"] == "2026-10-28T16:00:00+00:00"
    unreadable = invites.extract([], '<a href="https://track.example/click?u=abc">Add to Calendar</a>', "")
    assert unreadable["invites"] == [] and unreadable["add_to_calendar"]
    assert unreadable["links"] == [{"url": "https://track.example/click?u=abc", "text": "Add to Calendar"}]


def test_recruiter_invitations_go_on_the_calendar_without_asking(steward):
    run(steward.cycle(force=True))
    ics, start_utc = _outlook_invite()
    _deliver(steward, 500, "dana.lee@acme-cloud.example", "Invitation: Technical interview - Senior Platform Engineer",
             "Hi Alex, please find the invite for your technical interview for the Senior Platform Engineer role.",
             ics=ics)
    run(steward.cycle(force=True))
    cal = sandbox_json(steward, "calendar.json")["events"]
    added = [e for e in cal if e.get("ical_uid") == "040000008200E00074C5B7101A82E008-acme-1"]
    assert len(added) == 1 and added[0]["start"] == start_utc and added[0]["attendees"] == []
    auto = steward.db.query_one("SELECT * FROM drafts WHERE channel = 'calendar' AND summary LIKE 'Added to your calendar%'")
    assert auto["status"] == "executed" and auto["route"] == "auto_approve"  # nobody asked you
    ev_id = auto["event_id"]
    for d in steward.db.query("SELECT * FROM drafts WHERE event_id = ? AND id != ?", (ev_id, auto["id"])):
        assert not any(a["type"] in ("create_event", "rsvp_event") for a in loads(d["actions_json"], []))
    assert steward.db.query("SELECT 1 FROM ledger WHERE kind = 'auto_execution' AND draft_id = ?", (auto["id"],))

    # The same invitation again (a reminder, a forward): not added twice.
    _deliver(steward, 501, "dana.lee@acme-cloud.example", "Reminder: Technical interview", "See you soon.", ics=ics)
    run(steward.cycle(force=True))
    cal = sandbox_json(steward, "calendar.json")["events"]
    assert len([e for e in cal if e.get("ical_uid") == "040000008200E00074C5B7101A82E008-acme-1"]) == 1
    assert any("already on your calendar, not added again" in e["message"] for e in activity.recent(steward.db))

    # Already on the calendar under another UID (same time and title): not added either.
    other = ics.replace("UID:040000008200E00074C5B7101A82E008-acme-1", "UID:another-copy")
    _deliver(steward, 502, "dana.lee@acme-cloud.example", "Updated invitation: Technical interview", "Same time.",
             ics=other)
    run(steward.cycle(force=True))
    assert len([e for e in sandbox_json(steward, "calendar.json")["events"] if e["start"] == start_utc]) == 1


# -- logs, queue, execute -------------------------------------------------------------
def test_queue_logs_and_execute_now(workspace):
    data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    local = utcnow().astimezone(tz(data["profile"]["timezone"]))
    closed = (local.hour + 2) % 24
    data["linkedin"]["business_hours"] = f"{closed:02d}:00-{closed:02d}:01"  # LinkedIn is outside business hours
    workspace.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    app = create_app(load_config(workspace), watch=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        client.post("/api/poll", headers=h)
        b = client.get("/api/briefing").json()
        li = next(v for v in b["needs_decision"] + b["high_priority"] if v["channel"] == "linkedin")
        client.post(f"/api/drafts/{li['id']}/decision", json={"decision": "approve"}, headers=h)
        assert client.post("/api/go", headers=h).json()["deferred"] >= 1

        queue = client.get("/api/queue").json()["items"]
        item = next(q for q in queue if q["id"] == li["id"])
        assert item["status"] == "deferred" and item["can_execute"] and "outside business hours" in item["reason"]
        assert item["status_label"].startswith("Deferred until")
        logs = client.get("/api/logs").json()
        assert any(x.get("draft_id") == li["id"] and x["can_execute"] for x in logs["blockers"])
        messages = [e["message"] for e in logs["entries"]]
        assert any(m.startswith("Check (you pressed Check now") for m in messages)
        assert any(m.startswith("You pressed Go") for m in messages)
        assert any("deferred, LinkedIn pacing: outside business hours" in m for m in messages)

        assert client.post(f"/api/drafts/{li['id']}/execute").status_code == 403  # needs the page token
        res = client.post(f"/api/drafts/{li['id']}/execute", headers=h).json()
        assert res["status"] == "executed"
        cfg = load_config(workspace)
        assert json.loads((cfg.sandbox_path / "linkedin.json").read_text(encoding="utf-8"))["sent"]
        assert next(q for q in client.get("/api/queue").json()["items"] if q["id"] == li["id"])["status_label"] == "Done"
        assert any("you pressed Execute" in e["message"] for e in client.get("/api/logs").json()["entries"])
        assert client.post(f"/api/drafts/{li['id']}/execute", headers=h).status_code == 400  # only deferred items
