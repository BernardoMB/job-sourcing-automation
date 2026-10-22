"""Reconciliation: is each open item still needed?

You can always bypass Job Sourcer: answer a recruiter yourself on LinkedIn or by
email, or answer an invitation in your calendar. On every check, the open items
of each source that was just checked are compared with what really happened:

- LinkedIn: the conversation shows a message from you after theirs. If it is the
  reply Job Sourcer drafted, the item is done; otherwise you answered yourself and
  the item is closed (nothing is sent).
- Email: your Sent folder has a reply to that thread (or to that person, same
  subject) that Job Sourcer did not send: closed.
- Calendar: you already answered the invitation: closed.
- Replies handed to the linkedin-toolkit are synced with its queue: sent, failed
  (with its reason), or rejected.

Every closed item says why (Queue and Logs tabs) and goes to the ledger.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from steward import activity, pipeline
from steward.db import loads
from steward.mcp_hub import ExecutorCapability, MCPHub, MCPToolError
from steward.timeutil import fmt_local, now_iso, parse_iso, to_iso, utcnow

OPEN = ("pending", "approved", "edited", "queued", "deferred", "failed")


def _actions(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in (loads(draft["final_actions_json"], None) or loads(draft["actions_json"], []))
            if not a.get("skip")]


def _same_text(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", (s or "")).strip().lower()[:120]  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def _subject(s: str) -> str:
    return re.sub(r"^((re|fwd?|aw|rv)\s*:\s*)+", "", (s or "").strip(), flags=re.IGNORECASE).lower()


def close(st, draft: dict[str, Any], reason: str, status: str = "superseded", level: str = "info",
          kind: str = "reconcile") -> None:
    """Take an item out of the queue for good, saying why (ledger kind: reconcile, or discard when you chose to)."""
    st.db.update("drafts", draft["id"], {"status": status, "status_reason": reason, "closed_at": now_iso()})
    st.db.execute("UPDATE executions SET status = 'discarded', updated_at = ? WHERE draft_id = ? AND status = "
                  "'deferred'", (now_iso(), draft["id"]))
    st.ledger.append(kind, {"status": status, "reason": reason, "previous_status": draft["status"],
                            "summary": draft["summary"], "actions": [a["type"] for a in _actions(draft)]},
                     event_id=draft["event_id"], draft_id=draft["id"])
    activity.record(st.db, "reconcile" if kind == "reconcile" else "action", f"{draft['summary']}: {reason}", level,
                    draft_id=draft["id"], event_id=draft["event_id"])


def _mark_sent(st, draft: dict[str, Any], reason: str) -> None:
    st.db.update("drafts", draft["id"], {"status": "executed", "status_reason": reason,
                                          "executed_at": draft["executed_at"] or now_iso()})
    st.db.execute("UPDATE executions SET status = 'success', updated_at = ? WHERE draft_id = ? AND action_type = "
                  "'send_linkedin' AND status != 'success'", (now_iso(), draft["id"]))
    st.ledger.append("reconcile", {"status": "executed", "reason": reason, "previous_status": draft["status"],
                                   "summary": draft["summary"]}, event_id=draft["event_id"], draft_id=draft["id"])
    activity.record(st.db, "reconcile", f"{draft['summary']}: {reason}", draft_id=draft["id"], event_id=draft["event_id"])


def _record_manual(st, draft: dict[str, Any], channel: str, thread_key: str, recipient: str, subject: str, body: str,
                   external_id: str, sent_at: str) -> None:
    if external_id and st.db.query_one("SELECT 1 FROM outbound WHERE external_id = ?", (external_id,)):
        return
    st.db.insert("outbound", {"draft_id": None, "channel": channel, "thread_key": thread_key, "recipient": recipient,
                              "subject": subject, "body": body, "external_id": external_id, "sent_at": sent_at})
    if channel == "email" and external_id and thread_key:
        st.db.execute("INSERT OR IGNORE INTO message_ids (message_id, thread_key) VALUES (?, ?)",
                      (external_id, thread_key))
    pipeline.record_outbound(st.db, draft["opportunity_id"], f"You replied yourself on {channel}")


def handed_off(st, days: int = 7) -> list[dict[str, Any]]:
    """Replies handed to the linkedin-toolkit queue, with their queue id."""
    rows = st.db.query("SELECT * FROM executions WHERE status = 'handed_off' AND updated_at >= ?",
                       (to_iso(utcnow() - timedelta(days=days)),))
    return [{**r, "queue_id": (loads(r["result_json"], {}) or {}).get("queue_id", "")} for r in rows]


async def toolkit_items(hub: MCPHub) -> dict[str, dict[str, Any]] | None:
    res = await hub.call("linkedin", "toolkit_queue", {})
    return {i["id"]: i for i in res.get("items", [])} if res.get("supported") else None


async def sync_toolkit(st, hub: MCPHub) -> int:
    """What happened to replies handed to the linkedin-toolkit: sent, failed (and why), or rejected."""
    rows = handed_off(st)
    if not rows:
        return 0
    items = await toolkit_items(hub)
    if items is None:
        return 0
    changed = 0
    for x in rows:
        item = items.get(x["queue_id"])
        draft = st.db.get_draft(x["draft_id"])
        if not item or not draft:
            continue
        if item["status"] == "sent":
            st.db.update("executions", x["id"], {"status": "success", "updated_at": now_iso()})
            _mark_sent(st, draft, "Sent by the LinkedIn toolkit.")
            changed += 1
        elif item["status"] == "failed":
            hint = " (its own business hours)" if item.get("error_code") == "OUTSIDE_BUSINESS_HOURS" else ""
            why = f"The LinkedIn toolkit could not send it{hint}: {item.get('error') or 'no reason given'}"
            st.db.update("executions", x["id"], {"status": "failed", "error": why[:1000], "updated_at": now_iso()})
            st.db.update("drafts", draft["id"], {"status": "failed",
                                                  "status_reason": f"{why} Re-execute or Discard it in the Queue tab."})
            activity.record(st.db, "action", f"{draft['summary']}: {why}", "error", draft_id=draft["id"])
            changed += 1
        elif item["status"] == "rejected":
            st.db.update("executions", x["id"], {"status": "failed", "error": "rejected in the LinkedIn toolkit",
                                                 "updated_at": now_iso()})
            close(st, draft, "Rejected in the LinkedIn toolkit, so it was not sent.", status="discarded")
            changed += 1
    return changed


async def toolkit_decide(hub: MCPHub, queue_id: str, approve: bool) -> str:
    """Send or drop one toolkit item. Returns '' on success, else why it could not (Copilot mode)."""
    try:
        await hub.call("linkedin", "toolkit_queue_decide", {"queue_ids": [queue_id], "approve": approve},
                       capability=ExecutorCapability())
        return ""
    except MCPToolError as exc:
        return str(exc)


async def linkedin(st, hub: MCPHub) -> int:
    """Close LinkedIn items you already answered in LinkedIn itself; mark ours done once they show up there."""
    waiting = {x["draft_id"] for x in handed_off(st)}
    drafts = [d for d in st.db.query(f"SELECT * FROM drafts WHERE channel = 'linkedin' AND (status IN {OPEN} OR "
                                     "(status = 'executed' AND id IN (SELECT draft_id FROM executions WHERE "
                                     "status = 'handed_off')))")]
    by_conv: dict[str, list[dict[str, Any]]] = {}
    for d in drafts:
        event = st.db.get_event(d["event_id"]) or {}
        send = next((a for a in _actions(d) if a["type"] == "send_linkedin"), None)
        conv = (send or {}).get("payload", {}).get("conversation_id") or event.get("thread_key")
        if conv:
            by_conv.setdefault(conv, []).append(d)
    closed = 0
    for conv, ds in by_conv.items():
        try:
            res = await hub.call("linkedin", "get_conversation", {"conversation_id": conv, "limit": 20})
        except MCPToolError:
            continue
        msgs = res.get("messages", [])
        for d in ds:
            event = st.db.get_event(d["event_id"]) or {}
            since = parse_iso(event.get("received_at")) or utcnow()
            mine = [m for m in msgs if m.get("from") == "me" and (parse_iso(m.get("sent_at")) or since) > since]
            if not mine:
                continue
            send = next((a for a in _actions(d) if a["type"] == "send_linkedin"), None)
            ours = (send or {}).get("payload", {}).get("text", "")
            last = mine[-1]
            when = fmt_local(last.get("sent_at"), st.cfg.profile.timezone)
            if ours and any(_same_text(m.get("text", ""), ours) for m in mine):
                if d["status"] != "executed" or d["id"] in waiting:
                    _mark_sent(st, d, f"Sent: it is in the LinkedIn conversation ({when}).")
                    if not st.db.query_one("SELECT 1 FROM outbound WHERE draft_id = ?", (d["id"],)):
                        _record_manual(st, d, "linkedin", conv, event.get("sender_name", ""), "", ours,
                                       f"linkedin:{conv}:{last.get('id', '')}", last.get("sent_at") or now_iso())
                    closed += 1
                continue
            reason = f"You already replied on LinkedIn ({when}), so this is no longer needed. Nothing was sent."
            if d["id"] in waiting:  # don't let the toolkit send a second answer
                qid = next((x["queue_id"] for x in handed_off(st) if x["draft_id"] == d["id"]), "")
                if qid and await toolkit_decide(hub, qid, approve=False):
                    reason += " Also reject it in the LinkedIn toolkit popup, or it may still be sent."
            close(st, d, reason)
            _record_manual(st, d, "linkedin", conv, event.get("sender_name", ""), "", last.get("text", ""),
                           f"linkedin:{conv}:{last.get('id', '')}", last.get("sent_at") or now_iso())
            closed += 1
    return closed


async def email(st, hub: MCPHub) -> int:
    """Close email items you already answered from your own mailbox."""
    drafts = st.db.query(f"SELECT d.*, e.received_at AS ev_at FROM drafts d JOIN events e ON e.id = d.event_id "
                         f"WHERE d.channel = 'email' AND d.status IN {OPEN}")
    if not drafts:
        return 0
    oldest = min(parse_iso(d["ev_at"]) or utcnow() for d in drafts)
    cache = st.db.kv_get("mail:sent_cache") or {}
    covered = parse_iso(cache.get("since"))
    if not cache or covered is None or covered > oldest:
        res = await hub.call("mail", "list_sent", {"since_uid": 0, "since": to_iso(oldest - timedelta(days=1))})
        cache = {"since": to_iso(oldest - timedelta(days=1)), "last_uid": res["last_uid"], "items": res["messages"]}
    else:
        res = await hub.call("mail", "list_sent", {"since_uid": int(cache.get("last_uid") or 0)})
        cache["items"] = (cache.get("items", []) + res["messages"])[-400:]
        cache["last_uid"] = res["last_uid"]
    st.db.kv_set("mail:sent_cache", cache)
    ours = {r["external_id"] for r in st.db.query("SELECT external_id FROM outbound WHERE external_id IS NOT NULL "
                                                    "AND draft_id IS NOT NULL")}
    closed = 0
    for d in drafts:
        event = st.db.get_event(d["event_id"]) or {}
        raw = loads(event.get("raw_json"), {}) or {}
        ids = {raw.get("message_id", "")} | {r["message_id"] for r in st.db.query(
            "SELECT message_id FROM message_ids WHERE thread_key = ?", (event.get("thread_key") or "-",))}
        ids.discard("")
        since = (parse_iso(event.get("received_at")) or utcnow()) - timedelta(minutes=1)
        sender, subject = (event.get("sender_address") or "").lower(), _subject(event.get("subject", ""))
        hit = next((s for s in cache["items"]
                    if s.get("message_id") not in ours and (parse_iso(s.get("date")) or since) >= since
                    and (s.get("in_reply_to") in ids or ids & set(s.get("references") or [])
                         or (sender and sender in s.get("to", []) and subject and _subject(s.get("subject")) == subject))),
                   None)
        if not hit:
            continue
        when = fmt_local(hit.get("date"), st.cfg.profile.timezone)
        close(st, d, f"You already replied by email ({when}), so this is no longer needed. Nothing was sent.")
        _record_manual(st, d, "email", event.get("thread_key") or "", ", ".join(hit.get("to", [])),
                       hit.get("subject", ""), "", hit.get("message_id", ""), hit.get("date") or now_iso())
        closed += 1
    return closed


async def calendar(st, hub: MCPHub) -> int:
    """Close invitation answers you already gave in your calendar."""
    drafts = [d for d in st.db.query(f"SELECT * FROM drafts WHERE status IN {OPEN}")
              if any(a["type"] == "rsvp_event" for a in _actions(d))]
    if not drafts:
        return 0
    waiting = {i["id"] for i in (await hub.call("calendar", "pending_invites", {"days": 60})).get("invites", [])}
    closed = 0
    for d in drafts:
        rsvp = next(a for a in _actions(d) if a["type"] == "rsvp_event")
        if rsvp["payload"].get("event_id") not in waiting:
            close(st, d, "You already answered this invitation in your calendar, so this is no longer needed.")
            closed += 1
    return closed


async def run(st, hub: MCPHub, checked: dict[str, str], errors: list[str]) -> int:
    """Reconcile the sources checked in this cycle. Returns how many items changed."""
    def was_checked(source: str) -> bool:
        return checked.get(source, "not due") not in ("not due", "off", "failed")

    changed = 0
    steps = [("linkedin", sync_toolkit), ("linkedin", linkedin), ("email", email), ("calendar", calendar)]
    for source, step in steps:
        if not was_checked(source):
            continue
        try:
            changed += await step(st, hub)
        except (MCPToolError, OSError) as exc:
            errors.append(f"reconcile {source}: {exc}")
    return changed

