"""Your to-do list: things only you can do (assessments, forms, documents, applications).

Job Sourcer suggests a task whenever a message asks you to do something yourself;
you accept or dismiss it. You can also add tasks by hand and tick them off.
Tasks with a deadline appear under "Upcoming meetings & deadlines" and get
reminders. Every change is written to the ledger.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from steward.config import Config
from steward.db import Database
from steward.ledger import Ledger
from steward.taxonomy import TASK_KINDS
from steward.timeutil import fmt_local, now_iso, parse_iso, to_iso, utcnow

KINDS = TASK_KINDS
OPEN = ("open", "suggested")


class TaskError(ValueError):
    pass


def _due(value: str | None, tz_name: str) -> str | None:
    if not value:
        return None
    d = parse_iso(value, tz_name)
    if d is None:
        raise TaskError(f"Could not read the due date {value!r}")
    return to_iso(d)


def add(cfg: Config, db: Database, ledger: Ledger, title: str, *, details: str = "", kind: str = "other",
        link: str = "", due_at: str | None = None, opportunity_id: int | None = None, origin: str = "you",
        status: str = "open", event_id: int | None = None, draft_id: int | None = None) -> int:
    title = (title or "").strip()
    if not title:
        raise TaskError("A task needs a title")
    stamp = now_iso()
    task_id = db.insert("tasks", {
        "title": title[:300], "details": (details or "").strip()[:4000] or None,
        "kind": kind if kind in KINDS else "other", "link": (link or "").strip() or None,
        "due_at": _due(due_at, cfg.profile.timezone), "status": status, "origin": origin,
        "opportunity_id": opportunity_id, "event_id": event_id, "draft_id": draft_id,
        "created_at": stamp, "updated_at": stamp,
    })
    ledger.append("task", {"change": "suggested" if status == "suggested" else "added", "title": title,
                           "origin": origin, "due_at": due_at or "", "kind": kind},
                  event_id=event_id, draft_id=draft_id)
    return task_id


def suggest_from_classification(cfg: Config, db: Database, ledger: Ledger, cls: dict[str, Any], event_id: int,
                                opportunity_id: int | None) -> list[int]:
    """Turn 'please fill in this form by Friday' into a suggested task (not duplicated).

    Assessments skip the suggestion step: they become open tasks right away, so their deadline is never missed."""
    created = []
    for t in cls.get("candidate_tasks", []) or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        dup = db.query_one(
            "SELECT id FROM tasks WHERE lower(title) = ? AND ifnull(opportunity_id, 0) = ? AND status != 'dismissed'",
            (title.lower(), opportunity_id or 0))
        assessment = t.get("kind") == "assessment"
        if assessment and not dup:  # the assessment detector may already have recorded it from this email
            dup = db.query_one("SELECT * FROM tasks WHERE kind = 'assessment' AND status != 'dismissed' AND "
                               "event_id = ?", (event_id,))
            if dup:  # it read the page; the classifier may have read a deadline or link it missed
                fill = {k: v for k, v in (("due_at", t.get("due")), ("link", t.get("link"))) if v and not dup[k]}
                if fill:
                    try:
                        act(cfg, db, ledger, dup["id"], "update", fill)
                    except TaskError:
                        pass
        if dup:
            continue
        try:
            created.append(add(cfg, db, ledger, title, details=t.get("details", ""), kind=t.get("kind", "other"),
                               link=t.get("link", ""), due_at=t.get("due") or None, opportunity_id=opportunity_id,
                               origin="steward", status="open" if assessment else "suggested", event_id=event_id))
        except TaskError:
            continue
    return created


def link_draft(db: Database, event_id: int, draft_id: int) -> None:
    db.execute("UPDATE tasks SET draft_id = ? WHERE event_id = ? AND draft_id IS NULL", (draft_id, event_id))


def act(cfg: Config, db: Database, ledger: Ledger, task_id: int, action: str,
        fields: dict[str, Any] | None = None) -> dict[str, Any]:
    task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        raise TaskError("No such task")
    stamp = now_iso()
    updates: dict[str, Any] = {"updated_at": stamp}
    if action == "accept":
        updates["status"] = "open"
    elif action == "dismiss":
        updates["status"] = "dismissed"
    elif action == "done":
        updates.update({"status": "done", "completed_at": stamp})
    elif action == "reopen":
        updates.update({"status": "open", "completed_at": None})
    elif action == "update":
        f = fields or {}
        if "title" in f:
            if not str(f["title"]).strip():
                raise TaskError("A task needs a title")
            updates["title"] = str(f["title"]).strip()[:300]
        if "details" in f:
            updates["details"] = str(f["details"] or "").strip() or None
        if "link" in f:
            updates["link"] = str(f["link"] or "").strip() or None
        if "kind" in f and f["kind"] in KINDS:
            updates["kind"] = f["kind"]
        if "due_at" in f:
            updates["due_at"] = _due(f["due_at"], cfg.profile.timezone)
        if "opportunity_id" in f:
            updates["opportunity_id"] = f["opportunity_id"] or None
    else:
        raise TaskError("action must be accept, dismiss, done, reopen, or update")
    db.update("tasks", task_id, updates)
    ledger.append("task", {"change": action, "title": updates.get("title", task["title"]),
                           **({"fields": sorted(k for k in updates if k != "updated_at")} if action == "update" else {})},
                  event_id=task["event_id"], draft_id=task["draft_id"])
    return db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)) or {}


def view(cfg: Config, db: Database, t: dict[str, Any]) -> dict[str, Any]:
    opp = db.get_opportunity(t.get("opportunity_id"))
    due = parse_iso(t.get("due_at"))
    hours = (due - utcnow()).total_seconds() / 3600 if due else None
    return {
        **t,
        "kind_label": KINDS.get(t["kind"], t["kind"]),
        "company": (opp or {}).get("company") or "",
        "role": (opp or {}).get("role") or "",
        "due_label": fmt_local(due, cfg.profile.timezone) if due else "",
        "overdue": bool(due and hours is not None and hours < 0 and t["status"] in OPEN),
        "due_soon": bool(due and hours is not None and 0 <= hours <= 48 and t["status"] in OPEN),
    }


def listing(cfg: Config, db: Database, include_dismissed: bool = False) -> list[dict[str, Any]]:
    where = "" if include_dismissed else "WHERE status != 'dismissed'"
    rows = db.query(f"SELECT * FROM tasks {where} ORDER BY CASE status WHEN 'suggested' THEN 0 WHEN 'open' THEN 1 "
                    "ELSE 2 END, due_at IS NULL, due_at, id DESC")
    return [view(cfg, db, t) for t in rows]


def for_event(cfg: Config, db: Database, event_id: int) -> list[dict[str, Any]]:
    return [view(cfg, db, t) for t in db.query("SELECT * FROM tasks WHERE event_id = ? AND status != 'dismissed' "
                                               "ORDER BY id", (event_id,))]


def deadlines(cfg: Config, db: Database, days: int = 14) -> list[dict[str, Any]]:
    """Open tasks due within `days` (and anything overdue), soonest first."""
    horizon = to_iso(utcnow() + timedelta(days=days))
    rows = db.query("SELECT * FROM tasks WHERE status = 'open' AND due_at IS NOT NULL AND due_at <= ? "
                    "ORDER BY due_at", (horizon,))
    return [view(cfg, db, t) for t in rows]
