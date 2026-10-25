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


def test_acknowledgments_that_only_describe_later_steps_are_still_filed():
    from steward.acknowledgments import is_application_ack

    filed = {
        # Real wordings that used to slip into the pipeline.
        "We received your application for a position at Simpat Tech":
            "You have successfully submitted your application. Someone will review your qualifications shortly. "
            "Should your profile match our requirements, we will be in touch to schedule the next steps.",
        "Thanks for applying for Full-Stack Engineer":
            "Thank you for applying. We received your information and will contact you if there is a good match. If "
            "your profile is a good match, you will soon receive an invitation to complete a one-way video interview. "
            "We look forward to receiving and reviewing your video interview soon!",
        "Thank you: we received your application to join Clara":
            "We correctly received your application. We'll review your application. If you're not selected for this "
            "position, keep an eye on our jobs. Please do not reply: unfortunately replies will not be read.",
        "Senior Fullstack Engineer - DaCodes":
            "We have received your application. Should your profile align with any of our open positions, we will "
            "reach out to\nyou to schedule an initial interview.",
        "Thanks for applying to DaCodes":
            "Your application was submitted successfully. Here's a copy of your application data: PERSONAL "
            "INFORMATION ... Implemented risk assessment and compliance modules. Took a HackerRank test.",
    }
    kept = {  # they ask something of you now, or turn you down: never filed silently
        "Thank you for applying - assessment": "Thank you for applying! Please complete the assessment by Friday.",
        "Thanks for applying - book": "Thanks for applying. Please schedule your interview here: https://calendly.com/x",
        "Your application": "Thank you for applying. Unfortunately, we will not be moving forward with your application.",
        "Update": "Thank you for your application. We have decided to move forward with other candidates.",
    }
    for subject, body in filed.items():
        assert is_application_ack({"subject": subject, "body_text": body}), subject
    for subject, body in kept.items():
        assert not is_application_ack({"subject": subject, "body_text": body}), subject
