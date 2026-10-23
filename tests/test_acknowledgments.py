"""'Thank you for applying' emails are marked as read and moved to the Jobs label."""

from __future__ import annotations

import json

from steward import activity

from .conftest import run, sandbox_json


def _deliver(steward, uid: int, sender: str, subject: str, body: str) -> None:
    box = sandbox_json(steward, "mailbox.json")
    box["messages"].append({"uid": uid, "message_id": f"<m{uid}@example.test>", "from_name": "Careers",
                            "from_email": sender, "to": [steward.cfg.profile.email], "cc": [],
                            "date": "2026-10-22T10:00:00+00:00", "in_reply_to": "", "references": [],
                            "subject": subject, "body_text": body})
    (steward.cfg.sandbox_path / "mailbox.json").write_text(json.dumps(box), encoding="utf-8")


def _message(steward, uid: int) -> dict:
    return next(m for m in sandbox_json(steward, "mailbox.json")["messages"] if m["uid"] == uid)


def test_application_acknowledgments_are_read_and_filed(steward):
    run(steward.cycle(force=True))
    _deliver(steward, 700, "careers@acme-cloud.example", "Thank you for your application!",
             "Hi Bernardo, we've received your application for the Senior Platform Engineer role. Our team will "
             "review your application and get back to you.")
    _deliver(steward, 701, "talent@nimbus.example", "Thank you for your application!",
             "We've received your application. Finish it with a short video interview in the next 48 hours.")
    summary = run(steward.cycle(force=True))

    ack = steward.db.query_one("SELECT * FROM events WHERE external_id = '<m700@example.test>'")
    assert ack["status"] == "ignored" and ack["error"].startswith("Application acknowledgment: marked as read and "
                                                                  "moved to Jobs")
    filed = _message(steward, 700)
    assert filed["seen"] and filed["labels"] == ["Jobs"] and filed["in_inbox"] is False
    assert not steward.db.query("SELECT 1 FROM drafts WHERE event_id = ?", (ack["id"],))  # nothing to answer
    assert summary["new_events"] == 1  # no chime for an acknowledgment: only the video-interview email counts

    # It asks you to do something, so it is not filed away: it goes through the normal flow.
    video = steward.db.query_one("SELECT * FROM events WHERE external_id = '<m701@example.test>'")
    assert video["status"] != "ignored" and "labels" not in _message(steward, 701)
    assert any("moved to Jobs" in e["message"] for e in activity.recent(steward.db))


def test_filing_can_be_switched_off(steward):
    steward.cfg.mail.file_application_acks = False
    run(steward.cycle(force=True))
    _deliver(steward, 710, "careers@acme-cloud.example", "Application received",
             "We have received your application for Data Engineer. We will review your application soon.")
    run(steward.cycle(force=True))
    assert "labels" not in _message(steward, 710)
