"""Assessments become tasks with deadlines; reconciliation with what you did yourself; Re-execute and Discard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from steward import activity, briefing, decisions
from steward import tasks as tasks_mod
from steward.config import load_config
from steward.db import loads
from steward.orchestrator import Steward
from steward.timeutil import now_iso, tz, utcnow
from steward.web.app import create_app

from .conftest import run, sandbox_json

FAKE = Path(__file__).with_name("fake_linkedin_toolkit.py")

HACKERRANK_HTML = """<p>We are pleased to invite you to participate in the assessment for the open position at Velozient.</p>
<p>Duration: 75 min</p><p>End Login Date/Time: 23 Oct 2030 01:50 PM -03 (America - Buenos Aires, Argentina)</p>
<a href="https://www.hackerrank.com/tests/abc123/login?b=xyz">Start Test</a>
<a href="https://candidatesupport.hackerrank.com/hc/en-us">FAQ</a>"""


def _write(steward, name: str, data) -> None:
    (steward.cfg.sandbox_path / name).write_text(json.dumps(data), encoding="utf-8")


def _deliver(steward, uid: int, sender: str, subject: str, body: str, **extra) -> None:
    box = sandbox_json(steward, "mailbox.json")
    box["messages"].append({"uid": uid, "message_id": f"<m{uid}@example.test>", "from_name": "", "from_email": sender,
                            "to": [steward.cfg.profile.email], "cc": [], "date": now_iso(), "in_reply_to": "",
                            "references": [], "subject": subject, "body_text": body, **extra})
    _write(steward, "mailbox.json", box)


# -- assessments --------------------------------------------------------------------
def test_an_online_assessment_is_never_missed(steward):
    run(steward.cycle(force=True))
    body = ("[2026] Cloud Engineer (Azure) - powered by HackerRank\nHello,\nWe are pleased to invite you to participate "
            "in the assessment for the open position at Velozient.\nDuration: 75 min\nEnd Login Date/Time: 23 Oct 2030 "
            "01:50 PM -03\n(America - Buenos Aires, Argentina)\nStart Test")
    _deliver(steward, 600, "support@hackerrankforwork.com", "Your HackerRank [2026] Cloud Engineer (Azure) Invitation",
             body, html=HACKERRANK_HTML)
    _deliver(steward, 601, "support@hackerrankforwork.com",
             "Your invite to [2026] Cloud Engineer (Azure) expires in 7 days",
             body.replace("We are pleased to invite you to participate in the assessment for the open position at "
                          "Velozient.", "This is a friendly reminder that you were previously invited."),
             html=HACKERRANK_HTML)
    run(steward.cycle(force=True))
    [task] = steward.db.query("SELECT * FROM tasks WHERE kind = 'assessment' AND title LIKE '%Cloud Engineer%'")
    assert task["status"] == "open" and task["origin"] == "steward"  # a task you tick off yourself, not a suggestion
    assert task["title"] == "Take the Velozient [2026] Cloud Engineer (Azure) assessment (HackerRank)"
    assert task["link"] == "https://www.hackerrank.com/tests/abc123/login?b=xyz"  # the button, not the FAQ
    assert task["due_at"] == "2030-10-23T16:50:00+00:00" and "duration 75 min" in task["details"]
    # Not answered by email (the platform's no-reply address), and listed with the deadlines.
    for d in steward.db.query("SELECT d.* FROM drafts d JOIN events e ON e.id = d.event_id WHERE e.sender_address "
                              "LIKE '%hackerrank%'"):
        assert not any(a["type"] == "send_email" for a in loads(d["actions_json"], []))
    log = [e["message"] for e in activity.recent(steward.db)]
    assert any(m.startswith("Assessment found: Take the Velozient") for m in log)
    assert any(m.startswith("Reminder for an assessment already in your tasks") for m in log)
    assert task["id"] in [t["id"] for t in tasks_mod.deadlines(steward.cfg, steward.db, days=3650)]


# -- reconciliation -----------------------------------------------------------------
def _pending(steward, channel: str) -> dict:
    return steward.db.query_one("SELECT * FROM drafts WHERE channel = ? AND status = 'pending' ORDER BY id",
                                (channel,))


def test_items_you_answered_yourself_are_closed(steward):
    run(steward.cycle(force=True))
    li, mail = _pending(steward, "linkedin"), _pending(steward, "email")
    li_event, mail_event = steward.db.get_event(li["event_id"]), steward.db.get_event(mail["event_id"])

    # You replied in LinkedIn itself, and from your own mailbox.
    data = sandbox_json(steward, "linkedin.json")
    conv = next(c for c in data["conversations"] if c["id"] == li_event["thread_key"])
    conv["messages"].append({"id": "m-mine", "from": "me", "text": "Thanks! Talk tomorrow.", "sent_at": now_iso()})
    _write(steward, "linkedin.json", data)
    box = sandbox_json(steward, "mailbox.json")
    box["sent_by_me"] = [{"uid": 1, "message_id": "<my-own-reply@example.test>", "to": [mail_event["sender_address"]],
                          "subject": f"Re: {mail_event['subject']}", "date": now_iso(),
                          "in_reply_to": loads(mail_event["raw_json"], {})["message_id"], "references": []}]
    _write(steward, "mailbox.json", box)

    summary = run(steward.cycle(force=True))
    assert summary["reconciled"] >= 2
    li_now, mail_now = steward.db.get_draft(li["id"]), steward.db.get_draft(mail["id"])
    assert li_now["status"] == "superseded" and li_now["status_reason"].startswith("You already replied on LinkedIn")
    assert mail_now["status"] == "superseded" and mail_now["status_reason"].startswith("You already replied by email")
    assert sandbox_json(steward, "linkedin.json")["sent"] == [] and sandbox_json(steward, "outbox.json")["sent"] == []
    assert steward.db.query_one("SELECT 1 FROM outbound WHERE external_id = '<my-own-reply@example.test>'")
    assert {e["payload"]["status"] for e in steward.ledger.entries("reconcile")} == {"superseded"}
    assert briefing.build(steward.cfg, steward.db)["counts"]["undecided"] == \
        steward.db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'pending'")

    # A reply Job Sourcer drafted that shows up in the conversation (sent by hand, or late by the toolkit) is done.
    other = _pending(steward, "linkedin")
    text = next(a for a in loads(other["actions_json"], []) if a["type"] == "send_linkedin")["payload"]["text"]
    steward.db.update("drafts", other["id"], {"status": "failed", "status_reason": "toolkit failed"})
    data = sandbox_json(steward, "linkedin.json")
    conv = next(c for c in data["conversations"] if c["id"] == steward.db.get_event(other["event_id"])["thread_key"])
    conv["messages"].append({"id": "m-ours", "from": "me", "text": text, "sent_at": now_iso()})
    _write(steward, "linkedin.json", data)
    run(steward.cycle(force=True))
    done = steward.db.get_draft(other["id"])
    assert done["status"] == "executed" and done["status_reason"].startswith("Sent: it is in the LinkedIn conversation")


# -- Re-execute and Discard ----------------------------------------------------------
def _app(workspace, hours_closed: bool):
    data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    if hours_closed:
        closed = (utcnow().astimezone(tz(data["profile"]["timezone"])).hour + 2) % 24
        data["linkedin"]["business_hours"] = f"{closed:02d}:00-{closed:02d}:01"
        workspace.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    client = TestClient(create_app(load_config(workspace), watch=False), base_url="http://127.0.0.1:8765")
    return client


def test_discard_and_reexecute_from_the_queue(workspace):
    with _app(workspace, hours_closed=True) as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        client.post("/api/poll", headers=h)
        b = client.get("/api/briefing").json()
        li = [v for v in b["needs_decision"] + b["high_priority"] if v["channel"] == "linkedin"]
        for v in li[:2]:
            client.post(f"/api/drafts/{v['id']}/decision", json={"decision": "approve"}, headers=h)
        client.post("/api/go", headers=h)
        keep, drop = li[0]["id"], li[1]["id"]
        q = {x["id"]: x for x in client.get("/api/queue").json()["items"]}
        assert q[keep]["can_reexecute"] and q[keep]["can_discard"] and q[keep]["status"] == "deferred"

        assert client.post(f"/api/drafts/{drop}/discard").status_code == 403  # needs the page token
        res = client.post(f"/api/drafts/{drop}/discard", headers=h).json()
        assert res["status"] == "discarded"
        q = {x["id"]: x for x in client.get("/api/queue").json()["items"]}
        assert q[drop]["status_label"] == "Discarded" and not q[drop]["can_reexecute"] and not q[drop]["can_discard"]
        assert client.post(f"/api/drafts/{drop}/reexecute", headers=h).status_code == 400

        res = client.post(f"/api/drafts/{keep}/reexecute", headers=h).json()
        assert res["status"] == "executed"
        q = {x["id"]: x for x in client.get("/api/queue").json()["items"]}
        assert q[keep]["status_label"] == "Done" and not q[keep]["can_discard"]
        sent = json.loads((load_config(workspace).sandbox_path / "linkedin.json").read_text(encoding="utf-8"))["sent"]
        assert len(sent) == 1  # the discarded one never went out
        cfg = load_config(workspace)
        s = Steward(cfg)
        assert [e["draft_id"] for e in s.ledger.entries("discard")] == [drop]
        s.db.close()


def test_toolkit_queue_is_followed_and_reexecuted(workspace, tmp_path, monkeypatch):
    state = tmp_path / "toolkit_sends.json"
    queue = tmp_path / "toolkit_sends_queue.json"
    data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    data["profile"]["linkedin_url"] = "https://www.linkedin.com/in/alex-rivera-example/"
    data["linkedin"]["backend"] = "upstream"
    data["linkedin"]["upstream"] = {"command": sys.executable, "args": [str(FAKE)],
                                    "env": {"FAKE_LI_STATE": str(state), "FAKE_LI_MODE": "copilot"}, "tools": {}}
    workspace.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    s = Steward(load_config(workspace))
    run(s.cycle(force=True))
    draft = s.db.query_one("SELECT * FROM drafts WHERE channel = 'linkedin'")
    decisions.stage(s.db, draft["id"], "approve")
    run(s.go())
    assert s.db.get_draft(draft["id"])["status"] == "executed"  # handed to the toolkit (Copilot: waiting there)

    # Copilot mode: Re-execute cannot approve it for you, and says so.
    res = run(s.execute_now(draft["id"]))
    assert res["status"] == "waiting" and "Copilot" in res["reason"]

    # The toolkit failed it (outside its hours): the queue shows why, and Re-execute sends it again.
    queue.write_text(json.dumps({"q_1": {"status": "failed", "code": "OUTSIDE_BUSINESS_HOURS",
                                         "error": "Outside the configured window (9:00-18:00, weekdays only)."}}))
    s.db.kv_set("last_poll:linkedin", None)
    run(s.cycle())
    failed = s.db.get_draft(draft["id"])
    assert failed["status"] == "failed" and "its own business hours" in failed["status_reason"]
    res = run(s.execute_now(draft["id"]))
    assert res["status"] == "executed" and len(json.loads(state.read_text())) == 2

    # Later the toolkit reports it sent: Done.
    queue.write_text(json.dumps({"q_1": {"status": "failed"}, "q_2": {"status": "sent"}}))
    s.db.kv_set("last_poll:linkedin", None)
    run(s.cycle())
    done = s.db.get_draft(draft["id"])
    assert done["status"] == "executed" and done["status_reason"] == "Sent by the LinkedIn toolkit."
    s.db.close()
