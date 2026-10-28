"""LinkedIn messages are not skipped when a thread shows up late in the inbox list (InMails often do)."""

from __future__ import annotations

import json
from datetime import timedelta

from steward.timeutil import to_iso, utcnow
from steward.workspace import inject_email

from .conftest import run


def _add_thread(steward, conv_id: str, name: str, hours_ago: float, text: str) -> None:
    path = steward.cfg.sandbox_path / "linkedin.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["conversations"].append({
        "id": conv_id, "participant": {"name": name, "headline": "Recruiter", "profile_url": ""},
        "messages": [{"id": f"m-{conv_id}", "from": "them", "sent_at": to_iso(utcnow() - timedelta(hours=hours_ago)),
                      "text": text}]})
    path.write_text(json.dumps(data), encoding="utf-8")


def _events(steward, conv_id: str) -> list[dict]:
    return steward.db.query("SELECT * FROM events WHERE source = 'linkedin' AND thread_key = ?", (conv_id,))


def test_a_thread_that_shows_up_late_is_still_read(steward):
    run(steward.cycle(force=True))
    cursor = steward.db.kv_get("linkedin:since")
    # Its message is older than the newest one already read (it was not in the inbox list until now).
    _add_thread(steward, "li-late", "Valentina Example", 5, "Hi! We are hiring an AI Engineering Lead. "
                                                             "What's the best number to reach you?")
    run(steward.cycle(force=True))
    assert len(_events(steward, "li-late")) == 1
    assert steward.db.kv_get("linkedin:since") == cursor  # the cursor never moves back
    run(steward.cycle(force=True))
    assert len(_events(steward, "li-late")) == 1  # re-read, never recorded twice
    assert len(_events(steward, "li-201")) == 1


def test_a_notification_email_reads_its_thread_directly(steward):
    run(steward.cycle(force=True))
    # Older than the re-read window, so only the notification email can lead to it.
    _add_thread(steward, "li-old==", "Dana Example", 24 * 4, "Hello, are you open to a Staff Engineer role?")
    inject_email(steward.cfg, "Dana Example", "inmail-hit-reply@linkedin.com", "Dana Example sent you a message",
                 "Dana Example\n\nReply\nhttps://www.linkedin.com/messaging/thread/li-old%3D%3D/\n\nHello, are you "
                 "open to a Staff Engineer role?")
    run(steward.cycle(force=True))
    events = _events(steward, "li-old==")
    assert len(events) == 1 and events[0]["sender_name"] == "Dana Example"
    assert steward.db.kv_get("linkedin:notified") == {}  # read, so no longer pending
    run(steward.cycle(force=True))
    assert len(_events(steward, "li-old==")) == 1
