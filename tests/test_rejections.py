"""Rejection emails: the opportunity is closed (pipeline and Notion) and the email is moved to Trash."""

from __future__ import annotations

import json

from steward.workspace import inject_email

from .conftest import run, sandbox_json

PRIYA = ("Priya Shah", "priya.shah@northwind-payments.example")
TURNED_DOWN = ("Hi Alex,\n\nThank you for your time and interest in the Senior Platform Engineer role at Northwind "
               "Payments. Unfortunately, we have decided not to move forward with your application at this time.\n\n"
               "We wish you the best in your search.\n\nPriya")


def _northwind(steward) -> dict:
    return steward.db.query_one("SELECT * FROM opportunities WHERE company = 'Northwind Payments'")


def _message(steward, uid: int) -> dict:
    return next(m for m in sandbox_json(steward, "mailbox.json")["messages"] if int(m["uid"]) == uid)


def test_a_rejection_closes_the_opportunity_and_trashes_the_email(steward):
    run(steward.cycle(force=True))
    opp = _northwind(steward)
    assert opp["stage"] != "closed_lost"
    waiting = steward.db.query_one("SELECT id FROM drafts WHERE opportunity_id = ? AND status = 'pending'", (opp["id"],))
    assert waiting  # Priya's first email still waits for your reply

    uid = inject_email(steward.cfg, *PRIYA, "Your application to Northwind Payments", TURNED_DOWN)
    summary = run(steward.cycle(force=True))
    assert summary["errors"] == []

    opp = _northwind(steward)
    assert opp["stage"] == "closed_lost" and "not moving forward" in opp["next_action"]
    event = steward.db.query_one("SELECT * FROM events WHERE source = 'email' AND raw_json LIKE ?", (f'%"uid": {uid}%',))
    assert event["status"] == "info" and event["filed_at"] and "moved to Trash" in event["error"]
    assert not steward.db.query_one("SELECT 1 FROM drafts WHERE event_id = ?", (event["id"],))  # no reply drafted
    msg = _message(steward, uid)
    assert msg["trashed"] and not msg["in_inbox"]
    # Its earlier email is moot now: the reply that waited for it is withdrawn.
    assert steward.db.get_draft(waiting["id"])["status"] == "superseded"
    entry = next(e for e in steward.ledger.entries() if e["kind"] == "rejection")
    payload = entry["payload"] if isinstance(entry["payload"], dict) else json.loads(entry["payload"])
    assert payload["email"] == "trashed" and payload["stage"] == "closed_lost"
    assert steward.ledger.verify()["ok"]
    assert steward.db.query_one("SELECT 1 FROM activity WHERE message LIKE 'Rejection from Northwind Payments%'")


def test_a_rejection_that_asks_something_stays_with_you(steward):
    run(steward.cycle(force=True))
    uid = inject_email(steward.cfg, *PRIYA, "Your application to Northwind Payments",
                       TURNED_DOWN.replace("We wish you", "Would you like us to keep your CV on file for future "
                                                          "roles? We wish you"))
    run(steward.cycle(force=True))
    assert _northwind(steward)["stage"] == "closed_lost"  # still closed in the pipeline
    event = steward.db.query_one("SELECT * FROM events WHERE source = 'email' AND raw_json LIKE ?", (f'%"uid": {uid}%',))
    assert steward.db.query_one("SELECT 1 FROM drafts WHERE event_id = ?", (event["id"],))  # a reply for you to approve
    assert not _message(steward, uid).get("trashed")


def test_keeping_rejection_emails(steward):
    steward.cfg.mail.delete_rejections = False
    run(steward.cycle(force=True))
    uid = inject_email(steward.cfg, *PRIYA, "Your application to Northwind Payments", TURNED_DOWN)
    run(steward.cycle(force=True))
    assert _northwind(steward)["stage"] == "closed_lost"
    assert not _message(steward, uid).get("trashed")
    event = steward.db.query_one("SELECT * FROM events WHERE source = 'email' AND raw_json LIKE ?", (f'%"uid": {uid}%',))
    assert "kept" in event["error"] and not event["filed_at"]
