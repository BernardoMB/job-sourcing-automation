"""Once an item is handled, the emails about it are marked as read and moved to the Jobs label."""

from __future__ import annotations

import json

from steward import activity, decisions
from steward import tasks as tasks_mod
from steward.db import loads
from steward.timeutil import now_iso

from .conftest import run, sandbox_json


def _deliver(steward, uid: int, sender: str, subject: str, body: str, **extra) -> None:
    box = sandbox_json(steward, "mailbox.json")
    box["messages"].append({"uid": uid, "message_id": f"<m{uid}@example.test>", "from_name": "", "from_email": sender,
                            "to": [steward.cfg.profile.email], "cc": [], "date": now_iso(), "in_reply_to": "",
                            "references": [], "subject": subject, "body_text": body, **extra})
    (steward.cfg.sandbox_path / "mailbox.json").write_text(json.dumps(box), encoding="utf-8")


def _mail(steward, uid: int) -> dict:
    return next(m for m in sandbox_json(steward, "mailbox.json")["messages"] if m["uid"] == uid)


def _filed(steward, uid: int) -> bool:
    m = _mail(steward, uid)
    return bool(m.get("seen")) and m.get("labels") == ["Jobs"] and m.get("in_inbox") is False


def test_handled_items_take_their_emails_out_of_the_inbox(steward):
    convs = sandbox_json(steward, "linkedin.json")["conversations"]
    for i, conv in enumerate(convs):  # one notification email per LinkedIn conversation
        _deliver(steward, 810 + i, "messages-noreply@linkedin.com", "You have a new message",
                 f"{conv['participant']['name']} Reply https://www.linkedin.com/messaging/thread/{conv['id']}/ Hi!")
    _deliver(steward, 801, "jobs-noreply@linkedin.com", "30 new jobs for you", "Software Engineer at Acme")
    run(steward.cycle(force=True))

    by_conv = {conv["id"]: steward.db.query_one("SELECT d.* FROM drafts d JOIN events e ON e.id = d.event_id WHERE "
                                                "e.thread_key = ? AND d.status = 'pending'", (conv["id"],))
               for conv in convs}
    answered = next(i for i, c in enumerate(convs) if "[NEEDS YOUR INPUT" not in by_conv[c["id"]]["actions_json"])
    untouched = 1 - answered
    li = by_conv[convs[answered]["id"]]
    emails = [d for d in steward.db.query("SELECT d.*, e.raw_json AS ev_raw FROM drafts d JOIN events e "
                                          "ON e.id = d.event_id WHERE e.source = 'email' AND d.status = 'pending'")
              if "[NEEDS YOUR INPUT" not in d["actions_json"]]
    reply, reject = emails[0], emails[1]
    decisions.stage(steward.db, li["id"], "approve")
    decisions.stage(steward.db, reply["id"], "approve")
    decisions.stage(steward.db, reject["id"], "reject")
    assert not _filed(steward, 810 + answered)  # deciding is not enough: nothing has run yet
    result = run(steward.go())

    assert result["cleaned"] >= 3
    assert _filed(steward, 810 + answered)  # the LinkedIn notification for the conversation you answered
    assert not _filed(steward, 810 + untouched)  # the other conversation is still waiting for you
    assert not _filed(steward, 801)  # a job alert has nothing to do with it
    for d in (reply, reject):  # the email you answered, and the one you rejected
        assert _filed(steward, loads(d["ev_raw"])["uid"])
    assert any("moved to Jobs" in e["message"] for e in activity.recent(steward.db) if e["kind"] == "cleanup")

    # A newer message in a handled thread still needs you: it stays in the inbox.
    thread = steward.db.get_event(reply["event_id"])
    _deliver(steward, 802, thread["sender_address"], f"Re: {thread['subject']}", "One more question...",
             in_reply_to=loads(thread["raw_json"])["message_id"])
    run(steward.cycle(force=True))
    assert not _filed(steward, 802)


def test_an_email_with_an_open_task_stays(steward):
    run(steward.cycle(force=True))
    nimbus = steward.db.query_one("SELECT d.*, e.raw_json AS ev_raw FROM drafts d JOIN events e ON e.id = d.event_id "
                                  "WHERE e.subject LIKE '%assessment%'")
    task = steward.db.query_one("SELECT * FROM tasks WHERE event_id = ?", (nimbus["event_id"],))
    actions = [{"payload": {"body": a["payload"]["body"].replace("[NEEDS YOUR INPUT", "[filled")}}
               if a["type"] == "send_email" else {} for a in loads(nimbus["actions_json"], [])]
    decisions.stage(steward.db, nimbus["id"], "approve", actions)
    run(steward.go())
    uid = loads(nimbus["ev_raw"])["uid"]
    assert not _filed(steward, uid)  # the assessment is still to do: its email stays where you can see it

    tasks_mod.act(steward.cfg, steward.db, steward.ledger, task["id"], "done")
    steward.db.update("drafts", nimbus["id"], {"cleaned_at": None})
    run(steward.cycle(force=True))
    assert _filed(steward, uid)  # once the task is done, it is cleaned up too


def test_cleanup_can_be_switched_off(steward):
    steward.cfg.mail.cleanup_handled = False
    run(steward.cycle(force=True))
    d = next(d for d in steward.db.query("SELECT d.*, e.raw_json AS ev_raw FROM drafts d JOIN events e "
                                         "ON e.id = d.event_id WHERE e.source = 'email' AND d.status = 'pending'"))
    decisions.stage(steward.db, d["id"], "reject")
    run(steward.go())
    assert not _filed(steward, loads(d["ev_raw"])["uid"])
