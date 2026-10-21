"""End-to-end: real MCP server subprocesses, sandbox backends, offline LLM heuristics."""

from __future__ import annotations

import pytest

from steward import decisions
from steward.config import load_config
from steward.executor import Executor
from steward.mcp_hub import MCPHub
from steward.workspace import inject_email

from .conftest import drafts_by_company, run, sandbox_json


def test_walk_away_brief_decide_go(steward):
    summary = run(steward.cycle(force=True))
    assert summary["errors"] == []
    assert summary["ingested"]["email"] == 6 and summary["ingested"]["linkedin"] == 2
    assert summary["ingested"]["calendar"] == 1
    assert summary["auto_executed"] == 0  # nothing is earned yet: everything waits for you

    by = drafts_by_company(steward)
    assert set(by) >= {"Northwind Payments", "Lumen Analytics", "Brightpath Health", "Orbital Labs",
                       "Quanta Systems", "Helix Robotics", "Cedar Fintech", "Cobalt Cloud"}
    newsletter = steward.db.query_one("SELECT status FROM events WHERE subject LIKE '%digest%'")
    assert newsletter["status"] == "ignored"

    # Offer stage: autonomy revoked, high priority.
    assert by["Orbital Labs"]["priority"] == "high"
    assert "Offer room" in by["Orbital Labs"]["route_reasons_json"]
    # Salary: always escalated.
    assert by["Brightpath Health"]["priority"] == "high"
    # Rust is not in the CV: escalated instead of guessed.
    assert by["Helix Robotics"]["priority"] == "high"
    assert "[NEEDS YOUR INPUT" in by["Helix Robotics"]["actions_json"]
    # CV-grounded answers.
    assert "5.5 years" in by["Northwind Payments"]["actions_json"]
    # Proposed time collides with Team sync (+buffer): counter-proposal, no booking.
    lumen = steward.db.get_draft(by["Lumen Analytics"]["id"])
    assert "create_event" not in lumen["actions_json"] and "Would any of these work" in lumen["actions_json"]
    # Calendar invitation that fits: RSVP accept.
    assert "rsvp_event" in by["Cobalt Cloud"]["actions_json"]

    # Decide in one sitting, then go.
    for company in ("Northwind Payments", "Cedar Fintech", "Cobalt Cloud", "Quanta Systems"):
        decisions.stage(steward.db, by[company]["id"], "approve")
    decisions.stage(steward.db, by["Helix Robotics"]["id"], "reject", note="Say I am keen to learn Rust")
    result = run(steward.go())
    assert result["committed"] == {"approved": 4, "rejected": 1}
    assert result["executed"] == 4 and result["failed"] == 0

    sent = sandbox_json(steward, "outbox.json")["sent"]
    assert {m["to"] for m in sent} == {"priya.shah@northwind-payments.example", "elena.petrova@quanta-systems.example"}
    assert all(m["in_reply_to"] for m in sent)  # replies stay in the recruiter's thread
    li = sandbox_json(steward, "linkedin.json")["sent"]
    assert [m["conversation_id"] for m in li] == ["li-202"]
    cal = sandbox_json(steward, "calendar.json")["events"]
    assert next(e for e in cal if e["id"] == "cobalt-tech-1")["my_response"] == "accepted"

    assert steward.ledger.verify()["ok"]
    kinds = [e["kind"] for e in steward.ledger.entries(newest_first=False)]
    assert kinds.count("decision") == 5 and kinds.count("execution") == 4


def test_dont_ask_again_then_auto_handles_and_override_demotes(steward):
    run(steward.cycle(force=True))
    quanta = drafts_by_company(steward)["Quanta Systems"]
    res = decisions.dont_ask_again(steward.cfg, steward.db, steward.ledger, quanta["id"])
    assert res["tier"] == "auto_approve"
    run(steward.go())

    inject_email(steward.cfg, "Omar Haddad", "omar@vertex-robotics.example", "Backend role at Vertex Robotics",
                 "Hi Alex, I'm a recruiter at Vertex Robotics. We have a Senior Backend Engineer role open. "
                 "When are you free for a short call next week?")
    summary = run(steward.cycle(force=True))
    assert summary["auto_executed"] == 1
    auto = steward.db.query_one("SELECT * FROM drafts WHERE route = 'auto_approve'")
    assert auto["status"] == "executed" and auto["matched_rule_id"] == res["rule_id"]
    assert any(m["to"] == "omar@vertex-robotics.example" for m in sandbox_json(steward, "outbox.json")["sent"])
    assert steward.ledger.entries("auto_execution")

    # You flag it as wrong: the rule drops back to shadow (it only watches again).
    out = decisions.override(steward.cfg, steward.db, steward.ledger, auto["id"], "Too eager", "Shorter please.")
    assert out["demoted_to"] == "shadow"
    assert steward.db.get_rule(res["rule_id"])["status"] == "shadow"
    assert steward.ledger.entries("override")


def test_sensitive_topics_cannot_be_delegated(steward):
    run(steward.cycle(force=True))
    salary = drafts_by_company(steward)["Brightpath Health"]
    with pytest.raises(decisions.DecisionError):
        decisions.dont_ask_again(steward.cfg, steward.db, steward.ledger, salary["id"])


def _tech_email(steward, i: int) -> None:
    inject_email(steward.cfg, f"Recruiter {i}", f"talent{i}@acme{i}.example", f"Platform role at Acme{i}",
                 f"Hi Alex, I'm a recruiter at Acme{i}. We're hiring a Senior Platform Engineer. "
                 "Do you have experience with Terraform?")


def test_learning_loop_proposes_shadows_and_graduates(steward):
    run(steward.cycle(force=True))
    # Clear the seeded backlog so only our pattern remains.
    for d in steward.db.query("SELECT id FROM drafts WHERE status = 'pending'"):
        decisions.stage(steward.db, d["id"], "reject")
    run(steward.go())

    # 1) You approve three similar drafts without edits -> the learner proposes a rule.
    for i in range(3):
        _tech_email(steward, i)
    run(steward.cycle(force=True))
    for d in steward.db.query("SELECT id FROM drafts WHERE status = 'pending'"):
        decisions.stage(steward.db, d["id"], "approve")
    run(steward.go())
    prop = steward.db.query_one("SELECT * FROM proposals WHERE kind = 'new_rule' AND status = 'open'")
    assert prop is not None and "Terraform" in prop["title"]

    # 2) You accept it -> the rule starts in shadow mode (watches, still asks you).
    rule_id = decisions.decide_proposal(steward.cfg, steward.db, steward.ledger, prop["id"], True)["rule_id"]
    assert steward.db.get_rule(rule_id)["status"] == "shadow"

    # 3) It agrees with your next three decisions -> it graduates to active.
    for i in range(3, 6):
        _tech_email(steward, i)
    run(steward.cycle(force=True))
    pending = steward.db.query("SELECT * FROM drafts WHERE status = 'pending'")
    assert len(pending) == 3 and all(str(rule_id) in d["shadow_rule_ids_json"] for d in pending)
    for d in pending:
        decisions.stage(steward.db, d["id"], "approve")
    run(steward.go())
    assert steward.db.get_rule(rule_id)["status"] == "active"

    # 4) The next one is handled without asking.
    _tech_email(steward, 6)
    assert run(steward.cycle(force=True))["auto_executed"] == 1


def test_edits_become_a_preference_proposal(steward):
    for i in range(2):
        _tech_email(steward, 10 + i)
    run(steward.cycle(force=True))
    for d in steward.db.query("SELECT * FROM drafts WHERE status = 'pending' AND summary LIKE '%Acme%'"):
        actions = decisions.loads(d["actions_json"], [])
        short = "Hi, yes - 5.5 years of Terraform. Happy to chat this week.\nAlex"
        edited = [{"type": a["type"], "payload": {"body": short}} for a in actions]
        decisions.stage(steward.db, d["id"], "approve", edited)
    run(steward.go())
    prop = steward.db.query_one("SELECT * FROM proposals WHERE kind = 'new_preference'")
    assert prop is not None and "short" in prop["title"].lower()
    decisions.decide_proposal(steward.cfg, steward.db, steward.ledger, prop["id"], True)
    assert decisions.active_preferences(steward.db, ["tech_question"])


def test_executor_is_exactly_once_and_rechecks_calendar(steward):
    run(steward.cycle(force=True))
    by = drafts_by_company(steward)
    northwind = by["Northwind Payments"]
    decisions.stage(steward.db, northwind["id"], "approve")
    run(steward.go())
    before = len(sandbox_json(steward, "outbox.json")["sent"])

    async def again():
        async with MCPHub(steward.cfg) as hub:
            return await Executor(steward.cfg, steward.db, steward.ledger, hub).execute_draft(northwind["id"])

    res = run(again())
    assert all(o["status"] == "already_done" for o in res["outcomes"])
    assert len(sandbox_json(steward, "outbox.json")["sent"]) == before


def test_booking_conflict_at_execution_time_redrafts(steward):
    # The recruiter proposes a free time; something else gets booked before you say go.
    inject_email(steward.cfg, "Ana Ruiz", "ana@solstice.example", "Quick call",
                 "Hi Alex, I'm hiring for a Platform Engineer role at Solstice Data. "
                 "Would Tuesday, December 1 at 10:00 AM work for a 30-minute call?")
    run(steward.cycle(force=True))
    draft = steward.db.query_one("SELECT * FROM drafts WHERE summary LIKE '%Solstice%' OR summary LIKE '%Quick call%'")
    assert "create_event" in draft["actions_json"]
    decisions.stage(steward.db, draft["id"], "approve")
    cal_path = steward.cfg.sandbox_path / "calendar.json"
    import json

    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    booking = next(a for a in json.loads(draft["actions_json"]) if a["type"] == "create_event")["payload"]
    cal["events"].append({"id": "dentist", "title": "Dentist", "start": booking["start"], "end": booking["end"],
                          "attendees": [], "organizer": {}, "my_response": "accepted"})
    cal_path.write_text(json.dumps(cal), encoding="utf-8")
    result = run(steward.go())
    assert steward.db.get_draft(draft["id"])["status"] == "superseded"
    assert not [m for m in json.loads((steward.cfg.sandbox_path / "outbox.json").read_text())["sent"]
                if m["to"] == "ana@solstice.example"]  # the reply was held back too
    assert result["redrafted"] >= 1  # re-drafted with fresh availability


def test_reasoning_agents_cannot_send(workspace):
    cfg = load_config(workspace)

    async def attempt():
        async with MCPHub(cfg) as hub:
            await hub.call("mail", "send_email", {"to": ["x@example.com"], "subject": "s", "body": "b"})

    with pytest.raises(PermissionError):
        run(attempt())
