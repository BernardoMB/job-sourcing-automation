from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from steward.agents.router import route
from steward.config import Config
from steward.cvparse import CV
from steward.db import Database
from steward.ledger import Ledger
from steward.rules import create_rule, make_condition
from steward.scheduling import find_slots
from steward.workspace import PACKAGE_DATA

NY = ZoneInfo("America/New_York")


# -- ledger ----------------------------------------------------------------------
def test_ledger_chain_and_immutability(tmp_path):
    db = Database(tmp_path / "s.db")
    ledger = Ledger(db)
    for i in range(5):
        ledger.append("decision", {"decision": "approved", "i": i}, draft_id=i)
    assert ledger.verify() == {"ok": True, "entries": 5, "head": ledger.entries(limit=1)[0]["hash"]}
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE ledger SET payload_json = '{}' WHERE seq = 2")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM ledger WHERE seq = 2")
    # Someone bypasses the triggers: verification still catches it.
    db.execute("DROP TRIGGER ledger_no_update")
    db.execute("UPDATE ledger SET payload_json = '{\"decision\": \"rejected\"}' WHERE seq = 3")
    res = ledger.verify()
    assert res["ok"] is False and res["broken_at"] == 3


# -- CV analyst ------------------------------------------------------------------
@pytest.fixture(scope="module")
def cv() -> CV:
    return CV.load(PACKAGE_DATA / "cv.md", today=date(2026, 10, 21))


def test_cv_years_from_roles_and_stated(cv):
    k8s = cv.skill_experience("k8s")
    assert k8s["found"] and k8s["skill"] == "Kubernetes" and k8s["years"] == 5.5
    assert k8s["years_source"] == "computed_from_roles"
    assert cv.skill_experience("Go")["years"] == 5.0  # stated in the Skills section wins
    assert cv.skill_experience("golang")["skill"] == "Go"


def test_cv_never_invents(cv):
    rust = cv.skill_experience("Rust")
    assert rust["found"] is False and rust["years"] is None and rust["evidence"] == []


def test_cv_short_names_need_exact_casing():
    text = "# A\n## Experience\n### Eng — X\n*2020 – 2022*\n- We go to great lengths for customers.\n"
    assert CV.from_text(text, today=date(2026, 1, 1)).skill_experience("Go")["found"] is False


# -- scheduling ------------------------------------------------------------------
def test_slots_respect_buffers_hours_and_avoid():
    now = datetime(2026, 10, 21, 8, 0, tzinfo=NY)  # Wednesday
    busy = [{"start": datetime(2026, 10, 22, 10, 0, tzinfo=NY).isoformat(),
             "end": datetime(2026, 10, 22, 12, 0, tzinfo=NY).isoformat(), "title": "Busy"}]
    slots = find_slots(busy, now, now + timedelta(days=5), duration_minutes=30, tz_name="America/New_York",
                       working_days=[0, 1, 2, 3, 4], work_start="09:00", work_end="17:00", buffer_minutes=15,
                       min_notice_hours=18, avoid=["Fri 00:00-23:59"], max_slots=3, now=now)
    assert len(slots) == 3
    for s in slots:
        start = datetime.fromisoformat(s["start"])
        assert start.weekday() not in (4, 5, 6)  # Friday avoided, weekend not a working day
        assert 9 <= start.hour < 17
        assert start >= now + timedelta(hours=18)
        assert not (datetime(2026, 10, 22, 9, 45, tzinfo=NY) < start < datetime(2026, 10, 22, 12, 15, tzinfo=NY))


# -- decision router ---------------------------------------------------------------
def _route(db, cfg, *, cats, opp_stage="screening", conf=0.95, ja=0.9, unanswered=(), techs=(), risk="low"):
    event = {"source": "email", "sender_address": "r@x.example"}
    cls = {"categories": cats, "technologies": list(techs), "stage_signal": "none", "needs_reply": True,
           "risk_level": risk, "company": "X"}
    evidence = {"all_answered": not unanswered, "unanswered": list(unanswered)}
    drafted = {"confidence": conf, "reply_body": "Hi", "unanswered_questions": []}
    review = {"confidence": conf, "job_advancement_score": ja, "verdict": "send", "issues": [], "risks": []}
    return route(cfg, db, event=event, cls=cls, evidence=evidence, sched={"kind": "none"}, drafted=drafted,
                 review=review, opp={"stage": opp_stage, "company": "X"})


def test_router_asks_without_a_rule(tmp_path):
    db, cfg = Database(tmp_path / "s.db"), Config()
    d = _route(db, cfg, cats=["tech_question"])
    assert d.route == "ask" and d.band == "HIGH" and d.priority == "normal"


def test_router_rules_grant_autonomy_and_guardrails_win(tmp_path):
    db, cfg = Database(tmp_path / "s.db"), Config()
    create_rule(db, name="tech", condition=make_condition(["tech_question", "role_pitch", "salary_question"]),
                tier="auto_send", min_confidence=0.85, status="active", origin="user")
    assert _route(db, cfg, cats=["tech_question"]).route == "auto_send"
    # Medium risk can never be silent: downgraded to act-and-tell.
    assert _route(db, cfg, cats=["tech_question"], risk="medium").route == "auto_approve"
    # Offer stage revokes autonomy whatever the rule says.
    locked = _route(db, cfg, cats=["tech_question"], opp_stage="offer")
    assert locked.route == "ask" and locked.priority == "high" and "offer_lockout" in locked.flags
    # Sensitive categories always come back to you, even if a rule lists them.
    assert _route(db, cfg, cats=["salary_question"]).route == "ask"
    # Missing CV evidence forces escalation instead of a guess.
    gap = _route(db, cfg, cats=["tech_question"], unanswered=["Rust?"])
    assert gap.route == "ask" and gap.priority == "high"
    # Low confidence or low job-advancement never act alone.
    assert _route(db, cfg, cats=["tech_question"], conf=0.8).route == "ask"
    low = _route(db, cfg, cats=["tech_question"], ja=0.3)
    assert low.route == "ask" and "low_job_advancement" in low.flags
    # A message that also covers a category outside the rule is not covered.
    assert _route(db, cfg, cats=["tech_question", "logistics"]).route == "ask"


def test_router_technology_scoped_rule(tmp_path):
    db, cfg = Database(tmp_path / "s.db"), Config()
    create_rule(db, name="k8s", condition=make_condition(["tech_question"], ["Kubernetes", "Go"]),
                tier="auto_approve", min_confidence=0.85, status="active", origin="user")
    assert _route(db, cfg, cats=["tech_question"], techs=["k8s"]).route == "auto_approve"
    assert _route(db, cfg, cats=["tech_question"], techs=["Kubernetes", "Rust"]).route == "ask"


def test_shadow_rules_only_watch(tmp_path):
    db, cfg = Database(tmp_path / "s.db"), Config()
    rid = create_rule(db, name="s", condition=make_condition(["tech_question"]), tier="auto_approve",
                      min_confidence=0.85, status="shadow", origin="learner")
    d = _route(db, cfg, cats=["tech_question"])
    assert d.route == "ask" and d.shadow_rule_ids == [rid]
