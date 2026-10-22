"""Scheduling subagent: checks proposed times against the calendar MCP server and proposes slots.

Time comes from your calendar: checked here, and checked again by the executor
right before anything is booked.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from steward.agents.base import AgentContext
from steward.timeutil import fmt_range, parse_iso, to_iso, utcnow

SCHEDULING_CATEGORIES = {"availability_request", "interview_request"}


def label(ctx: AgentContext, start: str, end: str) -> str:
    return fmt_range(start, end, ctx.cfg.profile.timezone)


def meeting_title(ctx: AgentContext, cls: dict[str, Any], event: dict[str, Any]) -> str:
    return ctx.cfg.scheduling.meeting_title.format(
        company=cls.get("company") or "Recruiter",
        role=cls.get("role") or "intro call",
        recruiter=cls.get("recruiter_name") or event.get("sender_name") or "recruiter",
    )


async def slots_between(ctx: AgentContext, start, end, duration: int) -> list[dict[str, str]]:
    res = await ctx.hub.call("calendar", "find_free_slots", {
        "window_start": to_iso(start), "window_end": to_iso(end), "duration_minutes": duration,
        "max_slots": ctx.cfg.scheduling.max_slots,
    })
    return [{**s, "label": label(ctx, s["start"], s["end"])} for s in res.get("slots", [])]


async def plan(ctx: AgentContext, event: dict[str, Any], cls: dict[str, Any]) -> dict[str, Any]:
    sch = ctx.cfg.scheduling
    now = utcnow()
    cats = set(cls.get("categories", []))
    result: dict[str, Any] = {"kind": "none", "checks": [], "slots": [], "booking": None, "rsvp": None, "notes": []}
    if not ctx.hub.enabled("calendar"):
        if cats & SCHEDULING_CATEGORIES:
            result["notes"].append("Calendar integration is disabled; cannot check availability.")
            result["kind"] = "unknown"
        return result

    raw = event.get("raw", {})
    # 0) A job meeting changed: check the new time, or offer new times if it was dropped.
    if raw.get("kind") == "meeting_change":
        if raw.get("change") == "rescheduled":
            check = await ctx.hub.call("calendar", "check_availability", {
                "start": raw["start"], "end": raw["end"], "buffer_minutes": 0})
            others = [c for c in check.get("conflicts", []) if c.get("id") != raw.get("id")]
            result["checks"].append({"start": raw["start"], "end": raw["end"], "free": not others,
                                     "label": label(ctx, raw["start"], raw["end"]), "conflicts": others,
                                     "problems": check.get("problems", [])})
            if others:
                result["kind"] = "invite_conflict"
                s = parse_iso(raw["start"]) or now
                result["slots"] = await slots_between(ctx, max(now, s - timedelta(days=1)),
                                               s + timedelta(days=sch.lookahead_days), sch.default_duration_minutes)
        else:
            result["kind"] = "propose_slots"
            result["slots"] = await slots_between(ctx, now, now + timedelta(days=sch.lookahead_days),
                                           sch.default_duration_minutes)
        return result

    # 1) Calendar invitation sent by someone else.
    if event["source"] == "calendar":
        start, end = raw.get("start"), raw.get("end")
        check = await ctx.hub.call("calendar", "check_availability", {
            "start": start, "end": end, "buffer_minutes": 0})
        others = [c for c in check.get("conflicts", []) if c.get("id") != raw.get("id")]
        result["checks"].append({"start": start, "end": end, "label": label(ctx, start, end),
                                 "free": not others, "conflicts": others, "problems": check.get("problems", [])})
        if not others:
            result["kind"] = "invite_accept"
            result["rsvp"] = {"event_id": raw.get("id"), "response": "accepted"}
            result["accepted_label"] = label(ctx, start, end)
        else:
            result["kind"] = "invite_conflict"
            s = parse_iso(start) or now
            result["slots"] = await slots_between(ctx, max(now, s - timedelta(days=1)), s + timedelta(days=sch.lookahead_days),
                                           int(((parse_iso(end) or s) - s).total_seconds() // 60) or 30)
        return result

    # 2) The recruiter proposed concrete times.
    proposed = cls.get("proposed_times") or []
    if proposed:
        accepted = None
        for t in proposed:
            check = await ctx.hub.call("calendar", "check_availability", {
                "start": t["start"], "end": t["end"], "buffer_minutes": sch.buffer_minutes})
            s = parse_iso(t["start"])
            problems = list(check.get("problems", []))
            if s and s < now + timedelta(hours=sch.min_notice_hours):
                problems.append("too short notice")
            ok = check.get("free") and not problems
            result["checks"].append({**t, "label": label(ctx, t["start"], t["end"]), "free": bool(check.get("free")),
                                     "conflicts": check.get("conflicts", []), "problems": problems, "ok": ok})
            if ok and accepted is None:
                accepted = t
        if accepted:
            result["kind"] = "accept_proposed"
            result["accepted_label"] = label(ctx, accepted["start"], accepted["end"])
            if sch.book_on_accept and event.get("sender_address"):
                result["booking"] = {
                    "title": meeting_title(ctx, cls, event),
                    "start": accepted["start"], "end": accepted["end"],
                    "attendees": [event["sender_address"]],
                    "description": f"Scheduled by Job Sourcer. {cls.get('summary', '')}".strip(),
                    "add_video_link": sch.add_video_link and not cls.get("meeting_link"),
                    "location": cls.get("meeting_link", ""),
                }
        else:
            first = min(parse_iso(t["start"]) for t in proposed)
            duration = int(((parse_iso(proposed[0]["end"]) or first) - first).total_seconds() // 60) or \
                sch.default_duration_minutes
            window_start = max(now, first - timedelta(days=1))
            result["kind"] = "conflict_counter"
            result["slots"] = await slots_between(ctx, window_start, window_start + timedelta(days=sch.lookahead_days),
                                           duration)
        return result

    # 3) The recruiter asked for availability (or called and you missed it: offer times to talk).
    if cats & (SCHEDULING_CATEGORIES | {"call_notification"}) or \
            any(q["type"] == "availability" for q in cls.get("questions", [])):
        result["kind"] = "propose_slots"
        result["slots"] = await slots_between(ctx, now, now + timedelta(days=sch.lookahead_days), sch.default_duration_minutes)
        if not result["slots"]:
            result["notes"].append("No free slots found in the lookahead window.")
    return result
