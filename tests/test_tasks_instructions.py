"""Tasks and deadlines, your instructions on an item, and rules taught from an item."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from steward import briefing, decisions
from steward import tasks as tasks_mod
from steward.config import load_config
from steward.db import loads
from steward.timeutil import parse_iso, tz, utcnow
from steward.web.app import create_app

from .conftest import drafts_by_company, run, sandbox_json


def _long(d) -> str:
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day} at {d.strftime('%I:%M %p').lstrip('0')}"


def test_assessment_request_becomes_an_open_task_with_deadline(steward):
    run(steward.cycle(force=True))
    nimbus = drafts_by_company(steward)["Nimbus Data"]
    tasks = steward.db.query("SELECT * FROM tasks WHERE kind = 'assessment'")
    assert len(tasks) == 1  # one task, not one from the detector and another from the classifier
    task = tasks[0]
    assert task["status"] == "open" and task["link"].startswith("https://app.codility.com/")
    assert task["draft_id"] == nimbus["id"] and task["origin"] == "steward"
    due = parse_iso(task["due_at"]).astimezone(tz(steward.cfg.profile.timezone))
    assert (due.hour, due.minute) == (23, 59) and 3 <= (due - utcnow()).days <= 4
    view = briefing.draft_view(steward.cfg, steward.db, steward.db.get_draft(nimbus["id"]))
    assert view["tasks"][0]["status"] == "open"
    assert "take care of it" in nimbus["actions_json"]  # the reply acknowledges the assessment
    assert any(w.startswith("deadline") for w in loads(steward.db.get_draft(nimbus["id"])["priority_why_json"]))

    # An assessment never waits for you to accept it: its deadline is listed right away; done takes it off.
    b = briefing.build(steward.cfg, steward.db)
    assert [d["id"] for d in b["deadlines"]] == [task["id"]] and b["counts"]["tasks_open"] == 1
    tasks_mod.act(steward.cfg, steward.db, steward.ledger, task["id"], "done")
    assert briefing.build(steward.cfg, steward.db)["deadlines"] == []
    changes = [e["payload"]["change"] for e in steward.ledger.entries("task", newest_first=False)]
    assert changes == ["added", "done"]


def test_manual_tasks(steward):
    tid = tasks_mod.add(steward.cfg, steward.db, steward.ledger, "Fill in the visa questionnaire",
                        due_at="2030-01-15T17:00", link="https://forms.example/visa", kind="form")
    t = steward.db.query_one("SELECT * FROM tasks WHERE id = ?", (tid,))
    local = parse_iso(t["due_at"]).astimezone(tz(steward.cfg.profile.timezone))
    assert (local.hour, local.minute) == (17, 0) and t["status"] == "open" and t["origin"] == "you"
    tasks_mod.act(steward.cfg, steward.db, steward.ledger, tid, "update", {"title": "Fill in the TN questionnaire"})
    tasks_mod.act(steward.cfg, steward.db, steward.ledger, tid, "done")
    tasks_mod.act(steward.cfg, steward.db, steward.ledger, tid, "reopen")
    rows = tasks_mod.listing(steward.cfg, steward.db)
    assert rows[0]["title"] == "Fill in the TN questionnaire" and rows[0]["status"] == "open"
    tasks_mod.act(steward.cfg, steward.db, steward.ledger, tid, "dismiss")
    assert tasks_mod.listing(steward.cfg, steward.db) == []


def test_your_instruction_rewrites_the_item_and_waits_for_approval(steward):
    run(steward.cycle(force=True))
    cedar = drafts_by_company(steward)["Cedar Fintech"]
    res = run(steward.revise(cedar["id"], instruction="Decline politely, I only want remote roles"))
    old, new = steward.db.get_draft(cedar["id"]), steward.db.get_draft(res["draft_id"])
    assert old["status"] == "superseded" and new["status"] == "pending" and new["revised_from"] == cedar["id"]
    reply = json.loads(new["actions_json"])[-1]
    assert reply["type"] == "send_linkedin" and "pass on this one" in reply["payload"]["text"]
    assert "Rewritten from your instruction" in new["route_reasons_json"]
    assert steward.ledger.entries("instruction")

    decisions.stage(steward.db, new["id"], "approve")
    run(steward.go())
    sent = sandbox_json(steward, "linkedin.json")["sent"]
    assert any("pass on this one" in m["text"] for m in sent)
    decision = steward.ledger.entries("decision")[0]["payload"]
    assert decision["instructed"] and decision["instruction"].startswith("Decline politely")


def test_instruction_can_book_a_time_and_respects_conflicts(steward):
    run(steward.cycle(force=True))
    quanta = drafts_by_company(steward)["Quanta Systems"]
    zone = tz(steward.cfg.profile.timezone)
    day = utcnow().astimezone(zone) + timedelta(days=9)
    while day.weekday() != 1:  # a Tuesday, more than a week out
        day += timedelta(days=1)
    free = day.replace(hour=10, minute=0, second=0, microsecond=0)
    res = run(steward.revise(quanta["id"], instruction=f"Book {_long(free)} with her."))
    booking = next(a for a in json.loads(steward.db.get_draft(res["draft_id"])["actions_json"])
                   if a["type"] == "create_event")
    assert parse_iso(booking["payload"]["start"]) == free and res["warnings"] == []

    busy = day.replace(hour=14, minute=30, second=0, microsecond=0)  # the daily Team sync
    res2 = run(steward.revise(res["draft_id"], instruction=f"Book {_long(busy)} instead."))
    actions = json.loads(steward.db.get_draft(res2["draft_id"])["actions_json"])
    assert not any(a["type"] == "create_event" for a in actions)
    assert res2["warnings"] and "conflicts" in res2["warnings"][0]


def test_rule_from_an_item_is_scoped_and_can_redraft(steward):
    run(steward.cycle(force=True))
    northwind = drafts_by_company(steward)["Northwind Payments"]
    res = decisions.teach_rule(steward.db, steward.ledger, northwind["id"],
                               "Mention that I can start within four weeks.", "company")
    assert res["applies_to"] == "Northwind Payments"
    rule = "Mention that I can start within four weeks."
    assert rule in decisions.active_preferences(steward.db, ["tech_question"], "Northwind Payments")
    assert rule not in decisions.active_preferences(steward.db, ["tech_question"], "Lumen Analytics")
    out = run(steward.revise(northwind["id"], reason="Re-drafted with your new rule."))
    new = steward.db.get_draft(out["draft_id"])
    assert rule in loads(new["analysis_json"])["preferences_used"]
    assert steward.db.get_draft(northwind["id"])["status"] == "superseded"


def test_dashboard_endpoints_for_tasks_instructions_and_rules(workspace):
    cfg = load_config(workspace)
    app = create_app(cfg, watch=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        client.post("/api/poll", headers=h)
        T = client.get("/api/tasks").json()
        assert [(t["kind"], t["status"]) for t in T["tasks"]] == [("assessment", "open")] and "assessment" in T["kinds"]
        assert client.post("/api/tasks", json={"title": ""}, headers=h).status_code == 400
        tid = client.post("/api/tasks", json={"title": "Send portfolio", "due_at": "2030-02-01T12:00:00Z"},
                          headers=h).json()["id"]
        assert client.post(f"/api/tasks/{tid}", json={"action": "done"}, headers=h).json()["status"] == "done"

        b = client.get("/api/briefing").json()
        item = next(v for v in b["needs_decision"] + b["high_priority"] if "Cedar" in v["summary"])
        assert client.post(f"/api/drafts/{item['id']}/instruct", json={"instruction": ""}, headers=h).status_code == 400
        res = client.post(f"/api/drafts/{item['id']}/instruct", json={"instruction": "Decline politely."},
                          headers=h).json()
        b2 = client.get("/api/briefing").json()
        revised = next(v for v in b2["needs_decision"] + b2["high_priority"] if v["id"] == res["draft_id"])
        assert revised["revised"] and revised["instruction"] == "Decline politely."

        rule = client.post(f"/api/drafts/{res['draft_id']}/rule",
                           json={"text": "Always thank them by name.", "scope": "all", "redraft": True}, headers=h).json()
        assert rule["applies_to"] == "every message" and rule["revised"]["draft_id"] != res["draft_id"]
        assert any(p["text"] == "Always thank them by name." for p in client.get("/api/preferences").json())
