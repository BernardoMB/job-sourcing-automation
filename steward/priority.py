"""Priority score: which waiting decision deserves your attention first, and why.

Recomputed every cycle, so the queue re-orders itself as things change: a thread
moves to a later stage, a proposed time gets closer, a recruiter calls, or an
item has simply been waiting too long.
"""

from __future__ import annotations

from typing import Any

from steward.config import Config
from steward.db import Database, loads
from steward.taxonomy import stage_label
from steward.timeutil import fmt_local, parse_iso, utcnow

STAGE_WEIGHT = {"offer": 25, "final": 18, "interviewing": 12, "screening": 8, "contacted": 4, "new": 2}


def _times(draft: dict[str, Any], analysis: dict[str, Any], event: dict[str, Any]) -> list[Any]:
    out = []
    for a in loads(draft.get("final_actions_json"), None) or loads(draft.get("actions_json"), []) or []:
        if a.get("type") == "create_event" and not a.get("skip"):
            out.append(parse_iso(a["payload"].get("start")))
    for t in (analysis.get("classification") or {}).get("proposed_times", []) or []:
        out.append(parse_iso(t.get("start")))
    for s in (analysis.get("schedule") or {}).get("slots", []) or []:
        out.append(parse_iso(s.get("start")))
    raw = loads(event.get("raw_json"), {}) or {}
    if event.get("source") == "calendar":
        out.append(parse_iso(raw.get("start") or (raw.get("new") or {}).get("start")))
    now = utcnow()
    return sorted(t for t in out if t and t > now)


def compute(cfg: Config, db: Database, draft: dict[str, Any]) -> tuple[float, list[str]]:
    analysis = loads(draft.get("analysis_json"), {}) or {}
    cls = analysis.get("classification") or {}
    cats = set(cls.get("categories", []))
    event = db.get_event(draft["event_id"]) or {}
    opp = db.get_opportunity(draft.get("opportunity_id"))
    score, why = 0.0, []
    if draft.get("priority") == "high":
        score += 50
        why.append("needs your judgment")
    if opp:
        weight = STAGE_WEIGHT.get(opp["stage"], 0)
        score += weight
        if weight >= 12:
            why.append(f"{stage_label(opp['stage'])} stage")
    if "call_notification" in cats:
        score += 15
        why.append("they called you")
    if cats & {"interview_request", "calendar_invite", "meeting_change"}:
        score += 6
    if cls.get("urgency") == "high":
        score += 10
        why.append("marked urgent")
    due = sorted(d for d in (parse_iso(r["due_at"]) for r in db.query(
        "SELECT due_at FROM tasks WHERE event_id = ? AND status IN ('open', 'suggested') AND due_at IS NOT NULL",
        (draft["event_id"],))) if d)
    if due:
        hours = (due[0] - utcnow()).total_seconds() / 3600
        score += 20 if hours <= 48 else 8
        why.append(f"deadline {fmt_local(due[0], cfg.profile.timezone)}")
    soon = _times(draft, analysis, event)
    if soon:
        hours = (soon[0] - utcnow()).total_seconds() / 3600
        if hours <= 48:
            score += 15
            why.append(f"time-sensitive ({fmt_local(soon[0], cfg.profile.timezone)})")
        elif hours <= 168:
            score += 7
    received = parse_iso(event.get("received_at") or draft.get("created_at"))
    if received:
        waited = (utcnow() - received).total_seconds() / 3600
        score += min(12.0, max(waited, 0) / 4)
        if waited >= 24:
            why.append(f"waiting {int(waited // 24)}d")
    if draft.get("risk_level") == "high":
        score += 4
    return round(score, 1), why
