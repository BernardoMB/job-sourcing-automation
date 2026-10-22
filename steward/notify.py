"""Attention budgeting: Job Sourcer pings you only when it is worth your attention.

Rules: a digest when at least `min_pending` decisions are waiting or anything
high-priority arrived, never during quiet hours, at most once per cooldown; plus
a reminder shortly before each job-search meeting. Channels: ntfy (self-hostable
push) and Telegram, both optional. Every notification is also kept locally and
shown in the dashboard.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import httpx

from steward.config import Config
from steward.db import Database, dumps
from steward.mcp_hub import MCPHub, MCPToolError
from steward.scheduling import parse_hhmm
from steward.timeutil import fmt_local, now_iso, parse_iso, to_iso, tz, utcnow


def in_quiet_hours(cfg: Config) -> bool:
    try:
        a, b = cfg.notifications.quiet_hours.split("-")
        start, end = parse_hhmm(a), parse_hhmm(b)
    except ValueError:
        return False
    now = utcnow().astimezone(tz(cfg.profile.timezone)).time()
    return (start <= now or now < end) if start > end else (start <= now < end)


def send(cfg: Config, db: Database, kind: str, title: str, body: str, urgent: bool = False) -> list[str]:
    delivered: list[str] = []
    n = cfg.notifications
    for channel in n.channels:
        try:
            if channel == "ntfy" and n.ntfy_url:
                httpx.post(n.ntfy_url, content=body.encode("utf-8"),
                           headers={"Title": title, "Priority": "high" if urgent else "default", "Tags": "briefcase"},
                           timeout=15).raise_for_status()
                delivered.append("ntfy")
            elif channel == "telegram" and n.telegram_chat_id and os.environ.get(n.telegram_bot_token_env):
                token = os.environ[n.telegram_bot_token_env]
                httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": n.telegram_chat_id, "text": f"{title}\n\n{body}"}, timeout=15).raise_for_status()
                delivered.append("telegram")
        except httpx.HTTPError:
            continue
    db.insert("notifications", {"kind": kind, "title": title, "body": body, "channels_json": dumps(delivered),
                                "created_at": now_iso()})
    return delivered


def job_meetings(db: Database, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recruiter_emails = {r["recruiter_email"].lower() for r in db.query(
        "SELECT recruiter_email FROM opportunities WHERE recruiter_email IS NOT NULL AND recruiter_email != ''")}
    invite_ids = {r["thread_key"] for r in db.query("SELECT thread_key FROM events WHERE source = 'calendar'")}
    out = []
    for e in events:
        emails = {a.get("email", "").lower() for a in e.get("attendees", [])} | {
            (e.get("organizer") or {}).get("email", "").lower()}
        if e.get("created_by_steward") or e["id"] in invite_ids or emails & recruiter_emails:
            if e.get("my_response") != "declined":
                out.append(e)
    return out


async def upcoming(cfg: Config, db: Database, hub: MCPHub, days: int = 7) -> list[dict[str, Any]]:
    if not hub.enabled("calendar"):
        return []
    now = utcnow()
    try:
        res = await hub.call("calendar", "list_events", {"start": to_iso(now - timedelta(hours=1)),
                                                         "end": to_iso(now + timedelta(days=days))})
    except MCPToolError:
        return []
    meetings = job_meetings(db, res.get("events", []))
    for m in meetings:
        m["when"] = fmt_local(m["start"], cfg.profile.timezone)
    return meetings


async def maybe_notify(cfg: Config, db: Database, hub: MCPHub) -> list[str]:
    sent: list[str] = []
    n = cfg.notifications
    # Meeting reminders.
    reminded = set(db.kv_get("reminded_meetings", []) or [])
    for m in await upcoming(cfg, db, hub, days=1):
        start = parse_iso(m["start"])
        if not start or m["id"] in reminded:
            continue
        minutes = (start - utcnow()).total_seconds() / 60
        if 0 <= minutes <= n.meeting_reminder_minutes:
            send(cfg, db, "meeting", f"Meeting in {int(minutes)} min: {m['title']}",
                 f"{m['when']}\nJoin: {m.get('meeting_link') or m.get('location') or '(no link)'}", urgent=True)
            reminded.add(m["id"])
            sent.append(f"reminder:{m['id']}")
    db.kv_set("reminded_meetings", sorted(reminded)[-200:])

    # Deadline reminders: a day before and two hours before (quiet hours respected).
    reminded_tasks = db.kv_get("reminded_tasks", {}) or {}
    if not in_quiet_hours(cfg):
        for t in db.query("SELECT * FROM tasks WHERE status = 'open' AND due_at IS NOT NULL"):
            due = parse_iso(t["due_at"])
            if not due:
                continue
            hours = (due - utcnow()).total_seconds() / 3600
            stage = "2h" if 0 <= hours <= 2 else ("24h" if 0 <= hours <= 24 else "")
            key = str(t["id"])
            if not stage or stage in reminded_tasks.get(key, []):
                continue
            send(cfg, db, "deadline", f"Due {'in ' + str(max(int(hours), 0)) + ' h' if hours < 24 else 'tomorrow'}: "
                                      f"{t['title']}",
                 f"{fmt_local(due, cfg.profile.timezone)}" + (f"\n{t['link']}" if t.get("link") else ""), urgent=True)
            reminded_tasks.setdefault(key, []).append(stage)
            sent.append(f"deadline:{t['id']}")
        db.kv_set("reminded_tasks", reminded_tasks)

    # Decision digest.
    pending = db.query("SELECT priority FROM drafts WHERE status = 'pending'")
    high = sum(1 for p in pending if p["priority"] == "high")
    last = parse_iso(db.kv_get("last_digest_at"))
    cooled = last is None or utcnow() - last >= timedelta(minutes=n.cooldown_minutes)
    last_count = int(db.kv_get("last_digest_pending", 0) or 0)
    worth = len(pending) >= n.min_pending or high > 0
    if pending and worth and cooled and len(pending) != last_count and not in_quiet_hours(cfg):
        auto = db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'executed' AND route != 'ask' "
                         "AND acknowledged_at IS NULL") or 0
        body = (f"{len(pending)} decision(s) waiting ({high} high priority). "
                f"{auto} handled automatically since your last check.\n"
                f"Open http://{cfg.dashboard.host}:{cfg.dashboard.port} or run `steward brief`.")
        send(cfg, db, "digest", "Job Sourcer: your briefing is ready", body, urgent=high > 0)
        db.kv_set("last_digest_at", now_iso())
        db.kv_set("last_digest_pending", len(pending))
        sent.append("digest")
    return sent
