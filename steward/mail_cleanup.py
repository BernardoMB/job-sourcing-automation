"""Inbox cleanup: once an item is handled, its emails leave your inbox.

An item is handled when its reply really went out (not while it waits in the LinkedIn toolkit),
when you rejected it and pressed Go, when you discarded it, or when you had already answered it
yourself. Then every email that was only there to tell you about it is marked as read and moved
to your Jobs label:

- the email itself, and earlier emails in the same email thread (never ones that arrived after
  you handled it: those may need you);
- for a LinkedIn conversation, the "X sent you a message" notification emails that link to that
  conversation (or, without a link, name that person);
- for a meeting, the invitation email for it.

Runs on every check and right after Go / Execute / Discard. Each email is filed once.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from steward import activity, channels
from steward.db import loads
from steward.mcp_hub import ExecutorCapability, MCPHub, MCPToolError
from steward.timeutil import now_iso, parse_iso, to_iso, utcnow

LOOKBACK_DAYS = 14
THREAD_LINK = re.compile(r"linkedin\.com/messaging/thread/([A-Za-z0-9_%=+\-]+)", re.IGNORECASE)


def _handled(db) -> list[dict[str, Any]]:
    """Handled items whose emails have not been cleaned up yet, with when they were handled."""
    since = to_iso(utcnow() - timedelta(days=LOOKBACK_DAYS))
    rows = db.query(
        "SELECT * FROM drafts WHERE cleaned_at IS NULL AND (status IN ('executed', 'dismissed', 'discarded') "
        "OR (status = 'superseded' AND status_reason LIKE 'You already replied%')) "
        "AND COALESCE(executed_at, closed_at, decided_at, created_at) >= ?", (since,))
    out = []
    for d in rows:
        if d["status"] == "executed" and db.query_one(
                "SELECT 1 FROM executions WHERE draft_id = ? AND status IN ('handed_off', 'deferred', 'in_flight', "
                "'unknown', 'failed')", (d["id"],)):
            continue  # not done yet: waiting in the LinkedIn toolkit, deferred, or failed
        d["handled_at"] = d["executed_at"] or d["closed_at"] or d["decided_at"] or d["created_at"]
        out.append(d)
    return out


def _name_in(name: str, text: str) -> bool:
    words = [w for w in re.split(r"[\s,]+", (name or "").lower()) if len(w) > 1]
    low = text.lower()
    return bool(words) and (" ".join(words) in low or (len(words) > 1 and words[0] in low and words[-1] in low))


def related_emails(db, event: dict[str, Any], handled_at: str) -> list[dict[str, Any]]:
    """Unfiled emails that were only there to tell you about this item (none newer than when you handled it)."""
    until = handled_at or now_iso()
    candidates = db.query("SELECT * FROM events WHERE source = 'email' AND filed_at IS NULL AND received_at <= ?",
                          (until,))
    raw = loads(event.get("raw_json"), {}) or {}
    out = []
    if event["source"] == "email" or (event["source"] == "steward" and raw.get("channel", "email") == "email"):
        key = event.get("thread_key")
        out = [e for e in candidates if key and e["thread_key"] == key]
    elif event["source"] == "linkedin":
        conv = (event.get("thread_key") or "").rstrip("=")
        for e in candidates:
            if not channels.is_linkedin_address(e["sender_address"]):
                continue
            text = f"{e['subject']}\n{e['body_text'] or ''}"
            links = [m.rstrip("/").replace("%3D", "=").rstrip("=") for m in THREAD_LINK.findall(text)]
            if (conv and any(link.startswith(conv) or conv.startswith(link) for link in links)) or (not links and channels.is_linkedin_message_notice(
                    e["sender_address"], e["subject"] or "", e["body_text"] or "")
                    and _name_in(event.get("sender_name", ""), text)):
                out.append(e)
    elif event["source"] == "calendar":
        organizer = (event.get("sender_address") or "").lower()
        title = (event.get("subject") or "").strip().lower()
        start = parse_iso(raw.get("start"))
        for e in candidates:
            er = loads(e["raw_json"], {}) or {}
            if not (er.get("has_calendar_invite") or er.get("invites")) or e["sender_address"] != organizer:
                continue
            starts = [parse_iso(i.get("start")) for i in er.get("invites") or [] if i.get("start")]
            if starts and start:  # the invitation says when: it must be this meeting's time
                if start in starts:
                    out.append(e)
            elif title and title in (e["subject"] or "").lower():
                out.append(e)
    if event["source"] == "email" and not any(e["id"] == event["id"] for e in out) and not event.get("filed_at"):
        out.append(event)
    # An email whose task (an assessment, a form, a video interview) is still open still needs you.
    open_tasks = {r["event_id"] for r in db.query("SELECT event_id FROM tasks WHERE status IN ('open', 'suggested')")}
    return [e for e in out if (loads(e["raw_json"], {}) or {}).get("uid") is not None and e["id"] not in open_tasks]


async def sweep(st, hub: MCPHub) -> int:
    """File the emails of every newly handled item. Returns how many emails were filed."""
    cfg = st.cfg
    if not cfg.mail.cleanup_handled or not hub.enabled("mail"):
        return 0
    label = cfg.mail.applications_label
    filed = 0
    for draft in _handled(st.db):
        event = st.db.get_event(draft["event_id"]) or {}
        emails = related_emails(st.db, event, draft["handled_at"]) if event else []
        moved, problems = 0, []
        for e in emails:
            raw = loads(e["raw_json"], {}) or {}
            try:
                await hub.call("mail", "file_message", {"uid": int(raw["uid"]), "folder": raw.get("folder", ""),
                                                        "label": label, "mark_read": True},
                               capability=ExecutorCapability())
                moved += 1
            except (MCPToolError, ValueError) as exc:
                problems.append(str(exc)[:200])
            st.db.update("events", e["id"], {"filed_at": now_iso()})  # once: a message moved by hand stays moved
        st.db.update("drafts", draft["id"], {"cleaned_at": now_iso()})
        if moved or problems:
            activity.record(st.db, "cleanup", f"{draft['summary']}: handled, so {moved} email"
                            f"{'s' if moved != 1 else ''} about it marked as read and moved to {label}"
                            + (f" ({len(problems)} could not be moved: {problems[0]})" if problems else ""),
                            "warn" if problems else "info", draft_id=draft["id"], event_id=draft["event_id"])
        filed += moved
    return filed
