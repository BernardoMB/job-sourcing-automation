"""Emails offer every free day of the next two weeks, the pipeline shows each position's last action, and the
position review drafts a check-in for every position nothing is pending on."""

from __future__ import annotations

from datetime import timedelta

from steward import briefing, decisions
from steward.db import loads
from steward.timeutil import parse_iso, to_iso, tz, utcnow

from .conftest import drafts_by_company, run, sandbox_json


def _schedule(d: dict) -> dict:
    return (loads(d["analysis_json"], {}) or {}).get("schedule") or {}


def test_emails_list_free_hours_for_each_day_of_the_next_two_weeks(steward):
    run(steward.cycle(force=True))
    zone = tz(steward.cfg.profile.timezone)
    offering = [d for d in steward.db.query("SELECT * FROM drafts") if _schedule(d).get("slots")]
    by_email = [d for d in offering if d["channel"] == "email"]
    assert by_email
    for d in by_email:
        slots = _schedule(d)["slots"]
        days = {parse_iso(s["start"]).astimezone(zone).date() for s in slots}
        assert all(s.get("daily") for s in slots) and len(days) >= 8  # every working day, not three slots
        assert all(parse_iso(s["start"]).astimezone(zone).weekday() < 5 for s in slots)
        assert slots[0]["label"] in d["actions_json"] and slots[-1]["label"] in d["actions_json"]
        assert "availability for the next two weeks" in d["actions_json"]
    for d in offering:
        if d["channel"] in ("linkedin", "whatsapp"):
            assert len(_schedule(d)["slots"]) <= steward.cfg.scheduling.max_slots


def test_pipeline_shows_the_last_action_on_each_position(steward):
    run(steward.cycle(force=True))
    by = drafts_by_company(steward)
    opps = briefing.build(steward.cfg, steward.db)["pipeline"]["opportunities"]
    assert all(o.get("last_action") for o in opps)
    northwind = by["Northwind Payments"]
    assert steward.db.get_opportunity(northwind["opportunity_id"])["last_action"] == "Just received an email"
    decisions.stage(steward.db, northwind["id"], "approve")
    run(steward.go())
    assert steward.db.get_opportunity(northwind["opportunity_id"])["last_action"] == "Reply sent by email"


def test_review_follows_up_only_where_nothing_is_pending(steward):
    run(steward.cycle(force=True))
    by = drafts_by_company(steward)
    # Clear the briefing: Northwind and Quanta are answered, the rest you turned down.
    for company in ("Northwind Payments", "Quanta Systems"):
        decisions.stage(steward.db, by[company]["id"], "approve")
    for d in steward.db.query("SELECT * FROM drafts WHERE status = 'pending'"):
        decisions.stage(steward.db, d["id"], "reject")
    run(steward.go())
    quanta = steward.db.get_opportunity(by["Quanta Systems"]["opportunity_id"])
    steward.db.update("opportunities", quanta["id"], {"stage": "closed_lost"})

    # Everything just happened: nothing is stale yet.
    fresh = run(steward.review_positions())
    assert fresh["drafted"] == [] and all(x["reason"].startswith(("Not stale yet", "Closed", "You have an offer"))
                                          for x in fresh["skipped"])
    # Ten days of silence on every position except Cedar's.
    old = to_iso(utcnow() - timedelta(days=10))
    steward.db.execute("UPDATE opportunities SET last_inbound_at = ?, last_outbound_at = NULL WHERE company != ?",
                       (old, "Cedar Fintech"))

    res = run(steward.review_positions())
    drafted = {x["position"].split(" - ")[0]: x for x in res["drafted"]}
    skipped = {x["position"].split(" - ")[0]: x["reason"] for x in res["skipped"]}
    assert "Northwind Payments" in drafted
    assert skipped["Quanta Systems"].startswith("Closed")
    assert skipped["Cedar Fintech"].startswith("Not stale yet")
    # Cobalt's interview invitation is on the calendar: left alone.
    assert skipped["Cobalt Cloud"].startswith(("A meeting is on your calendar", "An invitation is waiting"))
    for x in res["drafted"]:
        d = steward.db.get_draft(x["draft_id"])
        assert d["status"] == "pending" and d["route"] == "ask"  # a decision item, nothing sent
        assert "still open" in d["actions_json"] and "update" in d["actions_json"]

    # Running it again adds nothing: the follow-ups are now waiting for you.
    again = run(steward.review_positions())
    assert again["drafted"] == []

    # Approving one sends it and updates the pipeline.
    follow = drafted["Northwind Payments"]
    sent_before = len(sandbox_json(steward, "outbox.json")["sent"])
    decisions.stage(steward.db, follow["draft_id"], "approve")
    run(steward.go())
    assert len(sandbox_json(steward, "outbox.json")["sent"]) == sent_before + 1
    opp = steward.db.get_opportunity(follow["id"])
    assert opp["last_action"].startswith("Follow-up sent by email (asked for an update")
