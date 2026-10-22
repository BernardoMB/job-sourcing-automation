"""Continuous watching: calls, meeting changes, re-prioritization, the dashboard API, adapters."""

from __future__ import annotations

import json
from datetime import date

from fastapi.testclient import TestClient

from steward import decisions
from steward.config import load_config
from steward.cvparse import CV
from steward.db import dumps, loads
from steward.linkedin_adapt import adapt_args, pick_upstream_tool
from steward.web.app import create_app
from steward.workspace import inject_email

from .conftest import drafts_by_company, run, sandbox_json

RESUME_SHAPE = """# **Jane Doe**

**Lead Engineer | Architect** | jane@example.com

## CORE SKILLS

**Backend:** .NET (C#), Python
**PROFESSIONAL EXPERIENCE**

## **Senior Developer — Acme Corp**

*Columbus, IN | Mar 2022 – Present*

> **Tech Stack:** Azure, Python

### Key Achievements

- Built the Go‑To‑Market tools platform with AWS Lambda.

## **Developer — Beta Inc**

*Mexico City | Jan 2016 – Feb 2022*

- Kubernetes operators.

## EDUCATION

**B.Sc. Actuarial Science** — *ITAM (2013–2018)* Statistics and risk modeling.
"""


def test_cv_resume_shapes():
    cv = CV.from_text(RESUME_SHAPE, today=date(2026, 10, 1))
    assert [r.title.split(" - ")[0] for r in cv.roles] == ["Senior Developer — Acme Corp", "Developer — Beta Inc"]
    assert cv.skill_experience("AWS")["found"]  # bullet under "### Key Achievements" still belongs to the role
    assert cv.skill_experience("AWS")["years"] == 4.5
    assert cv.skill_experience("Go")["found"] is False  # "Go-To-Market" is not the Go language
    assert cv.total_years() == 11.0  # the degree's 2013-2018 is not work experience
    assert "PROFESSIONAL EXPERIENCE" not in cv.stated_skills
    hidden = CV.from_text(RESUME_SHAPE, today=date(2026, 10, 1), )
    hidden.exclude_sections = ["Beta Inc"]
    k8s = hidden.skill_experience("Kubernetes")
    assert k8s["years"] and k8s["evidence"] == []  # years still count, the lines are never quoted


def test_missed_call_is_answered_in_the_recruiters_thread(steward):
    run(steward.cycle(force=True))
    inject_email(steward.cfg, "Google Voice", "voice-noreply@google.com", "New voicemail from Priya Shah",
                 "Hi Alex, this is Priya at Northwind Payments calling about the platform role. "
                 "Please call me back at (555) 010-2233. Thanks!")
    inject_email(steward.cfg, "Google Voice", "voice-noreply@google.com", "New voicemail from (555) 999-0000",
                 "Hello, I have a job opportunity for you, please call me back at (555) 999-0000.")
    run(steward.cycle(force=True))
    calls = steward.db.query("SELECT * FROM drafts WHERE analysis_json LIKE '%call_notification%' ORDER BY id")
    assert len(calls) == 2
    known, unknown = calls
    assert known["priority"] == "high"
    action = json.loads(known["actions_json"])[-1]
    assert action["type"] == "send_email" and action["payload"]["to"] == ["priya.shah@northwind-payments.example"]
    assert action["payload"]["in_reply_to"] == "<np-001@northwind-payments.example>"
    assert "Sorry I missed your call" in action["payload"]["body"]
    assert steward.db.get_opportunity(known["opportunity_id"])["company"] == "Northwind Payments"
    # Nobody to write to: a call-back item at the top of the briefing, nothing sent.
    assert unknown["priority"] == "high" and json.loads(unknown["actions_json"]) == []
    assert "Call back" in unknown["route_reasons_json"]


def test_cancelled_and_moved_meetings_are_noticed(steward):
    run(steward.cycle(force=True))  # snapshot includes the Cobalt Cloud interview invitation
    cal_path = steward.cfg.sandbox_path / "calendar.json"
    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    cal["events"] = [e for e in cal["events"] if e["id"] != "cobalt-tech-1"]
    cal_path.write_text(json.dumps(cal), encoding="utf-8")
    run(steward.cycle(force=True))
    ev = steward.db.query_one("SELECT * FROM events WHERE subject LIKE 'Cancelled:%'")
    assert ev is not None
    draft = steward.db.query_one("SELECT * FROM drafts WHERE event_id = ?", (ev["id"],))
    action = json.loads(draft["actions_json"])[-1]
    assert action["type"] == "send_email" and action["payload"]["to"] == ["nina.ross@cobalt-cloud.example"]
    assert "Here are a few times" in action["payload"]["body"]


def test_reprioritization_reacts_to_stage_and_clock(steward):
    run(steward.cycle(force=True))
    by = drafts_by_company(steward)
    cedar = by["Cedar Fintech"]
    assert cedar["priority"] == "normal" and cedar["priority_score"] is not None
    # You move Cedar Fintech to Offer in the pipeline: its waiting draft becomes high priority and loses autonomy.
    steward.db.update("opportunities", cedar["opportunity_id"], {"stage": "offer"})
    # The times offered in the Quanta draft are now in the past: it must be re-drafted.
    quanta = by["Quanta Systems"]
    analysis = loads(quanta["analysis_json"])
    analysis["schedule"]["slots"][0]["start"] = "2020-01-01T10:00:00+00:00"
    steward.db.update("drafts", quanta["id"], {"analysis_json": dumps(analysis)})
    summary = run(steward.cycle(force=True))
    assert summary["reprioritized"] == {"escalated": 1, "redrafted": 1}
    cedar_now = steward.db.get_draft(cedar["id"])
    assert cedar_now["priority"] == "high" and "Offer room" in cedar_now["route_reasons_json"]
    assert cedar_now["priority_score"] > cedar["priority_score"]
    assert steward.db.get_draft(quanta["id"])["status"] == "superseded"
    fresh = steward.db.query_one("SELECT * FROM drafts WHERE event_id = ? AND status = 'pending'", (quanta["event_id"],))
    assert fresh is not None


def test_dashboard_api(workspace):
    cfg = load_config(workspace)
    app = create_app(cfg, watch=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/").text
        token = page.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        assert client.post("/api/poll").status_code == 403  # no token: other websites cannot act
        assert client.get("/api/stream").status_code == 403
        evil = TestClient(app, base_url="http://evil.example")
        assert evil.get("/api/briefing").status_code == 403  # DNS-rebinding guard
        assert client.post("/api/poll", headers=h).json()["processed"] == 10
        b = client.get("/api/briefing").json()
        assert b["counts"]["undecided"] == 9 and b["loop"]["polls"]["email"] == 1
        scores = [v["priority_score"] for v in b["high_priority"]]
        assert scores == sorted(scores, reverse=True)

        # "Act on each approval immediately": approving sends right away, no Go needed.
        client.post("/api/settings", json={"execute_on_approve": True}, headers=h)
        northwind = next(v for v in b["needs_decision"] + b["high_priority"] if "Northwind" in v["summary"])
        res = client.post(f"/api/drafts/{northwind['id']}/decision", json={"decision": "approve"}, headers=h).json()
        assert res["executed"]["status"] == "executed"
        sent = json.loads((cfg.sandbox_path / "outbox.json").read_text(encoding="utf-8"))["sent"]
        assert [m["to"] for m in sent] == ["priya.shah@northwind-payments.example"]

        hist = client.get("/api/history").json()
        assert any(r["id"] == northwind["id"] and r["outcome"] == "done" and r["decided_by"] == "you" for r in hist)

        setup = client.get("/api/setup").json()
        assert {c["id"] for c in setup["checks"]} >= {"profile", "cv", "llm", "mail", "calendar", "notion"}
        assert client.post("/api/setup/secret", json={"name": "NOTION_TOKEN", "value": "secret_x"}, headers=h).json()["ok"]
        assert "NOTION_TOKEN=secret_x" in (workspace.parent / ".env").read_text(encoding="utf-8")
        assert client.get("/api/setup").json()["secrets"]["NOTION_TOKEN"] is True
        assert client.post("/api/setup/secret", json={"name": "PATH", "value": "x"}, headers=h).status_code == 400
        ok = client.post("/api/setup/config", json={"changes": {"profile.timezone": "America/Mexico_City"}}, headers=h)
        assert ok.status_code == 200 and load_config(workspace).profile.timezone == "America/Mexico_City"
        assert client.post("/api/setup/config", json={"changes": {"llm.max_tokens": 5}}, headers=h).status_code == 400
        assert client.post("/api/setup/probe/cv", headers=h).json()["ok"]


def test_linkedin_upstream_adapter():
    tools = {"linkedin_query_sql": {}, "inbox_list_conversations": {}, "get_conversation_messages": {},
             "send_message": {}, "send_connection_invite": {}, "profile_get": {}}
    assert pick_upstream_tool("list_conversations", tools, {}) == "inbox_list_conversations"
    assert pick_upstream_tool("get_conversation", tools, {}) == "get_conversation_messages"
    assert pick_upstream_tool("send_message", tools, {}) == "send_message"
    assert pick_upstream_tool("send_message", tools, {"send_message": "custom"}) == "custom"
    schema = {"properties": {"threadUrn": {"type": "string"}, "body": {"type": "string"}}}
    assert adapt_args(schema, {"conversation_id": "c1", "text": "hi"}) == {"threadUrn": "c1", "body": "hi"}
