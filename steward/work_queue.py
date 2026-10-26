"""The Queue tab and the "what's blocking" panel of the Logs tab.

The queue is every action that was decided but has not finished: waiting for Go,
queued, running, deferred (with when it will try again), failed, or handed to the
LinkedIn toolkit's own approval queue, plus what finished in the last day.
Blockers say, in plain words, why something that should happen has not.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from steward.config import Config
from steward.db import Database, loads
from steward.timeutil import fmt_local, parse_iso, to_iso, utcnow

ACTION_LABELS = {"send_email": "Email reply", "send_linkedin": "LinkedIn reply", "send_whatsapp": "WhatsApp reply",
                 "create_event": "Book meeting",
                 "rsvp_event": "Answer invitation", "add_to_calendar": "Add to calendar"}
ACTIVE = ("approved", "edited", "rejected", "queued", "executing", "deferred", "failed")
STAGED = ("approved", "edited", "rejected")  # decided in the briefing, confirmed (run) only when you press Go
PENDING = "Pending confirmation"


def queue_actions(db: Database, draft: dict[str, Any]) -> dict[str, bool]:
    """Which of Re-execute / Discard make sense for a queue item right now."""
    handed = bool(db.query_one("SELECT 1 FROM executions WHERE draft_id = ? AND status = 'handed_off'", (draft["id"],)))
    live = draft["status"] in ("approved", "edited", "queued", "deferred", "failed") or (
        draft["status"] == "executed" and handed)
    return {"can_reexecute": live, "can_discard": live}


def _due(next_attempt_at: str | None) -> bool:
    nxt = parse_iso(next_attempt_at)
    return nxt is None or nxt <= utcnow()


def go_counts(db: Database) -> dict[str, int]:
    """What pressing Go runs right now (decisions pending confirmation, queued items, deferred items whose
    time has come), and what it does not: deferred items still waiting for their time, and failed ones
    (Go never retries a failure; Re-execute or Discard it)."""
    staged = db.scalar(f"SELECT COUNT(*) FROM drafts WHERE status IN {STAGED}") or 0
    queued = db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'queued'") or 0
    due = waiting = 0
    for d in db.query("SELECT id FROM drafts WHERE status = 'deferred'"):
        row = db.query_one("SELECT next_attempt_at FROM executions WHERE draft_id = ? AND status = 'deferred' "
                           "ORDER BY id DESC LIMIT 1", (d["id"],))
        if _due(row["next_attempt_at"] if row else None):
            due += 1
        else:
            waiting += 1
    failed = db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'failed'") or 0
    return {"ready_to_go": staged + queued + due, "deferred_waiting": waiting, "needs_attention": failed}


def _target(action: dict[str, Any]) -> str:
    p = action.get("payload") or {}
    if action["type"] == "send_email":
        return ", ".join(p.get("to") or [])
    if action["type"] == "send_linkedin":
        return p.get("recipient") or "LinkedIn conversation"
    if action["type"] == "send_whatsapp":
        return p.get("recipient") or "WhatsApp chat"
    return p.get("title") or p.get("start", "")


def _decided_by(draft: dict[str, Any]) -> str:
    if draft["route"] == "ask":
        return "You"
    if draft["channel"] == "calendar" and not draft["matched_rule_id"]:
        return "Automatic (recruiter invitation)"
    return "Rule (act silently)" if draft["route"] == "auto_send" else "Rule (act and tell me)"


def items(cfg: Config, db: Database) -> list[dict[str, Any]]:
    since = to_iso(utcnow() - timedelta(days=1))
    week = to_iso(utcnow() - timedelta(days=7))
    rows = db.query(f"SELECT * FROM drafts WHERE status IN {ACTIVE} OR (status = 'executed' AND executed_at >= ?) "
                    "OR (status = 'executed' AND id IN (SELECT draft_id FROM executions WHERE status = 'handed_off' "
                    "AND updated_at >= ?)) OR (status IN ('discarded', 'superseded') AND closed_at >= ?) "
                    "ORDER BY id DESC", (since, week, week))
    tz_name = cfg.profile.timezone
    on_approve = bool(db.kv_get("settings", {}).get("execute_on_approve", cfg.autonomy.execute_on_approve))
    out = []
    for d in rows:
        actions = [a for a in (loads(d["final_actions_json"], None) or loads(d["actions_json"], [])) if not a.get("skip")]
        execs = {e["action_index"]: e for e in db.query("SELECT * FROM executions WHERE draft_id = ? ORDER BY id",
                                                          (d["id"],))}
        nxt = next((e["next_attempt_at"] for e in execs.values() if e["status"] == "deferred"), None)
        handed = any(e["status"] == "handed_off" for e in execs.values())
        status = d["status"]
        label = {
            "approved": PENDING if not on_approve else "Queued", "edited": PENDING if not on_approve else "Queued",
            "rejected": PENDING, "queued": "Queued", "executing": "Running", "failed": "Failed",
            "deferred": f"Deferred until {fmt_local(nxt, tz_name)}" if nxt else "Deferred",
            "executed": "Waiting in the LinkedIn toolkit" if handed else "Done",
            "discarded": "Discarded", "superseded": "No longer needed",
        }[status]
        updated = max([e["updated_at"] for e in execs.values()] + [d["closed_at"] or d["executed_at"] or d["decided_at"]
                                                                    or d["created_at"]])
        out.append({
            "id": d["id"], "summary": d["summary"], "channel": d["channel"], "status": status, "status_label": label,
            "reason": d["status_reason"] or (
                "You rejected it: nothing will be sent. Press Go to confirm." if status == "rejected" else
                "You approved it. Press Go to confirm, and it starts processing." if label == PENDING else
                "Approve it in the linkedin-toolkit extension popup (Copilot mode), or turn on its Autopilot."
                if handed else ""),
            "decided_by": _decided_by(d), "next_attempt_at": nxt, "updated_at": updated,
            "actions": [] if status == "rejected" else [
                {"type": ACTION_LABELS.get(a["type"], a["type"]), "target": _target(a),
                 "status": (execs.get(i) or {}).get("status", "not started"),
                 "error": (execs.get(i) or {}).get("error") or ""} for i, a in enumerate(actions)],
            "can_execute": status == "deferred", **queue_actions(db, d),
            **_go_group(status, label, nxt, actions),
        })
    return out


def _go_group(status: str, label: str, nxt: str | None, actions: list[dict[str, Any]]) -> dict[str, str]:
    """Which part of the Queue tab an item belongs to, and what pressing Go will do with it."""
    what = ", ".join(f"{ACTION_LABELS.get(a['type'], a['type']).lower()} to {_target(a)}" for a in actions) or "nothing"
    if status == "rejected":
        return {"group": "ready", "go_will": "Record your rejection. Nothing is sent."}
    if label == PENDING or status == "queued":
        return {"group": "ready", "go_will": f"Run: {what}."}
    if status == "deferred":
        return ({"group": "ready", "go_will": f"Retry now (its wait is over): {what}."} if _due(nxt) else
                {"group": "waiting", "go_will": "Nothing yet: it waits for its time (or press Execute)."})
    if status == "failed":
        return {"group": "attention", "go_will": "Nothing: Go never retries a failure. Re-execute or Discard it."}
    if status in ("executing",) or label == "Waiting in the LinkedIn toolkit":
        return {"group": "waiting", "go_will": "Nothing: it is already on its way."}
    if status in ("discarded", "superseded"):
        return {"group": "closed", "go_will": ""}
    return {"group": "done", "go_will": ""}


def blockers(cfg: Config, db: Database) -> list[dict[str, Any]]:
    """Why something that should happen has not: each with what to do about it."""
    tz_name = cfg.profile.timezone
    out: list[dict[str, Any]] = []
    queue = items(cfg, db)
    for q in queue:
        if q["status"] == "deferred":
            out.append({"level": "warn", "draft_id": q["id"], "can_execute": True,
                        "title": f"Deferred: {q['summary']}",
                        "detail": f"{q['reason'] or 'Waiting'}. Next try {fmt_local(q['next_attempt_at'], tz_name)}"
                                  if q["next_attempt_at"] else (q["reason"] or "Waiting.")})
        elif q["status"] == "failed":
            out.append({"level": "error", "draft_id": q["id"], "title": f"Failed: {q['summary']}", "can_reexecute": True,
                        "detail": q["reason"] or "See the Logs below."})
        elif q["status_label"] == "Waiting in the LinkedIn toolkit":
            out.append({"level": "warn", "draft_id": q["id"], "title": f"Waiting in the LinkedIn toolkit: {q['summary']}",
                        "detail": q["reason"]})
    staged = [q for q in queue if q["status_label"] == PENDING]
    if staged:
        out.append({"level": "info", "title": f"{len(staged)} decision{'s' if len(staged) != 1 else ''} pending "
                                              "confirmation",
                    "detail": "Decisions you made in the briefing run only when you press Go (or switch on 'Act on "
                              "each approval immediately').", "go": True})
    stuck = db.scalar("SELECT COUNT(*) FROM executions WHERE status = 'unknown'") or 0
    if stuck:
        out.append({"level": "error", "title": f"{stuck} interrupted action{'s' if stuck != 1 else ''}",
                    "detail": "An attempt stopped midway, so Job Sourcer will not retry it blindly. Check whether it "
                              "went out (Queue tab), then decide again."})
    pause = db.kv_get("llm_paused")
    if pause and (parse_iso(pause.get("until")) or utcnow()) > utcnow():
        out.append({"level": "warn", "title": f"Claude is paused until {fmt_local(pause['until'], tz_name)}",
                    "detail": f"{pause.get('reason', '')} New messages are collected and drafted after that."})
    health = db.kv_get("linkedin_health") or {}
    for problem in health.get("problems", []) if not health.get("ok", True) else []:
        out.append({"level": "error" if not health.get("read_ok", True) else "warn",
                    "title": "LinkedIn is not fully usable", "detail": problem, "linkedin": True})
    for err in db.kv_get("last_check_errors", []) or []:
        out.append({"level": "error", "title": "The last check had a problem", "detail": str(err)[:600]})
    waiting = db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'pending'") or 0
    if waiting:
        out.append({"level": "info", "title": f"{waiting} item{'s' if waiting != 1 else ''} waiting for your decision",
                    "detail": "Nothing is sent for them until you decide in the Briefing."})
    return out
