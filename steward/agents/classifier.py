"""Triage subagent: what is this message, what does it ask, what does it imply?"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from steward.agents.base import AgentContext, block, call_llm
from steward.agents.prompts import CLASSIFY_SYSTEM
from steward.llm.schemas import CLASSIFY_SCHEMA
from steward.taxonomy import CATEGORIES, QUESTION_TYPES, STAGE_SIGNALS, TASK_KINDS, risk_of
from steward.timeutil import parse_iso, to_iso, tz, utcnow


def render_thread(thread: list[dict[str, Any]], limit_chars: int = 6000) -> str:
    lines = []
    for m in thread[-8:]:
        who = "Candidate" if m.get("direction") == "out" else (m.get("sender") or "Recruiter")
        lines.append(f"[{m.get('at', '')}] {who}: {m.get('text', '')[:1500]}")
    text = "\n\n".join(lines)
    return text[-limit_chars:] if text else "(no earlier messages)"


def normalize(raw: dict[str, Any], cfg_tz: str) -> dict[str, Any]:
    cats = [c for c in raw.get("categories", []) if c in CATEGORIES]
    primary = raw.get("primary_category") if raw.get("primary_category") in CATEGORIES else (cats[0] if cats else "other")
    if primary not in cats:
        cats.insert(0, primary)
    if not raw.get("is_recruiter_related", True):
        cats, primary = ["not_recruiter"], "not_recruiter"
    questions = []
    for q in raw.get("questions", []) or []:
        questions.append({
            "text": str(q.get("text", ""))[:500],
            "type": q.get("type") if q.get("type") in QUESTION_TYPES else "other",
            "entities": [str(e) for e in q.get("entities", []) or []][:8],
            "search_terms": [str(e) for e in q.get("search_terms", []) or []][:8],
        })
    times = []
    for t in raw.get("proposed_times", []) or []:
        start = parse_iso(t.get("start"), cfg_tz)
        if not start:
            continue
        end = parse_iso(t.get("end"), cfg_tz)
        if not end or end <= start:
            end = start + timedelta(minutes=30)
        times.append({"start": to_iso(start), "end": to_iso(end)})
    out = {
        "is_recruiter_related": bool(raw.get("is_recruiter_related", True)),
        "needs_reply": bool(raw.get("needs_reply", True)),
        "summary": str(raw.get("summary", ""))[:300],
        "company": str(raw.get("company", "")).strip(),
        "role": str(raw.get("role", "")).strip(),
        "recruiter_name": str(raw.get("recruiter_name", "")).strip(),
        "phone_number": str(raw.get("phone_number", "")).strip(),
        "location": str(raw.get("location", "")).strip(),
        "categories": cats,
        "primary_category": primary,
        "questions": questions,
        "technologies": [str(t) for t in raw.get("technologies", []) or []][:20],
        "proposed_times": times,
        "meeting_link": str(raw.get("meeting_link", "")),
        "stage_signal": raw.get("stage_signal") if raw.get("stage_signal") in STAGE_SIGNALS else "none",
        "urgency": raw.get("urgency") if raw.get("urgency") in ("low", "normal", "high") else "normal",
        "suspicious": bool(raw.get("suspicious", False)),
        "candidate_tasks": [],
    }
    for t in raw.get("candidate_tasks", []) or []:
        title = str(t.get("title", "")).strip()
        if not title:
            continue
        due = parse_iso(t.get("due"), cfg_tz)
        out["candidate_tasks"].append({
            "title": title[:200], "kind": t.get("kind") if t.get("kind") in TASK_KINDS else "other",
            "due": to_iso(due) if due else "", "link": str(t.get("link", "")).strip(),
            "details": str(t.get("details", "")).strip()[:1500],
        })
    out["risk_level"] = risk_of(cats)
    return out


async def classify(ctx: AgentContext, event: dict[str, Any], thread: list[dict[str, Any]]) -> dict[str, Any]:
    zone = ctx.cfg.profile.timezone
    now_local = utcnow().astimezone(tz(zone))
    sender = f"{event.get('sender_name') or ''} <{event.get('sender_address') or ''}>".strip()
    user = "\n\n".join([
        block("now", now_local.isoformat(timespec="minutes") + f" ({now_local.strftime('%A')})"),
        block("candidate_timezone", zone),
        block("channel", event["source"]),
        block("from", sender),
        block("subject", event.get("subject") or "(none)"),
        block("thread_history", render_thread(thread)),
        block("message", event.get("body_text") or ""),
    ])
    context = {"event": event, "now": now_local.isoformat(), "timezone": zone, "thread": thread}
    raw = await call_llm(ctx, "classify", "classifier", event.get("id"), CLASSIFY_SYSTEM, user, CLASSIFY_SCHEMA, context)
    return normalize(raw, zone)
