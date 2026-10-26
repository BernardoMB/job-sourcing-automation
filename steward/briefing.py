"""The briefing: what happened while you were away, and what needs your judgment.

Sections, in the order you see them:
  High priority            salary, offer stage, missing CV evidence, low confidence
  Needs your decision      everything else that waits for you
  Handled automatically    actions taken under rules you activated (you can override)
  Suggested automation     rules and preferences the learner proposes
  Upcoming meetings        job-search meetings in the next 7 days, with join links
  Pipeline                 every opportunity by stage
"""

from __future__ import annotations

from typing import Any

from steward import tasks as tasks_mod
from steward import work_queue
from steward.agents.router import calibration
from steward.config import Config
from steward.db import Database, loads
from steward.rules import rule_view
from steward.taxonomy import STAGES, TIER_LABELS, stage_label
from steward.timeutil import ago, fmt_local, parse_iso, utcnow

OPEN = ("pending", "approved", "edited", "rejected")


def draft_view(cfg: Config, db: Database, d: dict[str, Any]) -> dict[str, Any]:
    event = db.get_event(d["event_id"]) or {}
    analysis = loads(d["analysis_json"], {}) or {}
    cls = analysis.get("classification", {}) or {}
    sched = analysis.get("schedule", {}) or {}
    review = analysis.get("review", {}) or {}
    evidence = analysis.get("evidence", {}) or {}
    actions = loads(d["final_actions_json"], None) or loads(d["actions_json"], []) or []
    opp = db.get_opportunity(d["opportunity_id"])
    hard = set(cfg.autonomy.hard_escalate_categories) | {"offer", "negotiation"}
    blocked = sorted(set(cls.get("categories", [])) & hard)
    rule = db.get_rule(d["matched_rule_id"]) if d["matched_rule_id"] else None
    return {
        "id": d["id"],
        "channel": d["channel"],
        "summary": d["summary"],
        "status": d["status"],
        "status_reason": d.get("status_reason"),
        "priority": d["priority"],
        "priority_score": d.get("priority_score") or 0,
        "priority_why": loads(d.get("priority_why_json"), []) or [],
        "route": d["route"],
        "route_label": TIER_LABELS.get(d["route"], d["route"]),
        "band": d["routing_band"],
        "confidence": d["confidence"],
        "job_advancement": d["job_advancement"],
        "risk": d["risk_level"],
        "reasons": loads(d["route_reasons_json"], []),
        "created_ago": ago(d["created_at"]),
        "created_at": d["created_at"],
        "executed_at": d.get("executed_at"),
        "event": {
            "source": event.get("source"),
            "sender_name": event.get("sender_name"),
            "sender_address": event.get("sender_address"),
            "subject": event.get("subject"),
            "body": event.get("body_text"),
            "received": fmt_local(event.get("received_at"), cfg.profile.timezone),
        },
        "actions": actions,
        "original_actions": loads(d["actions_json"], []),
        "categories": cls.get("categories", []),
        "questions": cls.get("questions", []),
        "technologies": cls.get("technologies", []),
        "evidence": evidence.get("items", []),
        "fit": evidence.get("fit", []),
        "schedule": {
            "kind": sched.get("kind"),
            "slots": [s.get("label") for s in sched.get("slots", [])],
            "checks": sched.get("checks", []),
            "accepted": sched.get("accepted_label"),
        },
        "review": {"verdict": review.get("verdict"), "issues": review.get("issues", []), "risks": review.get("risks", [])},
        "preferences_used": analysis.get("preferences_used", []),
        "examples_used": analysis.get("examples_used", 0),
        "rule": {"id": rule["id"], "name": rule["name"], "tier": rule["tier"]} if rule else None,
        "shadow_rules": loads(d["shadow_rule_ids_json"], []),
        "opportunity": {"id": opp["id"], "company": opp["company"], "role": opp["role"],
                        "stage": stage_label(opp["stage"])} if opp else None,
        "tasks": tasks_mod.for_event(cfg, db, d["event_id"]),
        "revised": bool(d.get("revised_from")),
        "instruction": analysis.get("instruction", ""),
        "dont_ask": {
            "allowed": not blocked,
            "blocked_reason": ("Job Sourcer always asks you about " + ", ".join(b.replace("_", " ") for b in blocked))
            if blocked else "",
            "categories": [c for c in cls.get("categories", []) if c != "not_recruiter"],
            "technologies": cls.get("technologies", []),
            "sender": event.get("sender_address", ""),
            "company": cls.get("company", ""),
        },
    }


def build(cfg: Config, db: Database, meetings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    last_go = db.kv_get("last_go_at")
    open_drafts = db.query(f"SELECT * FROM drafts WHERE status IN {OPEN} ORDER BY id")
    views = [draft_view(cfg, db, d) for d in open_drafts]
    views.sort(key=lambda v: (-(v["priority_score"] or 0), v["id"]))
    high = [v for v in views if v["priority"] == "high"]
    normal = [v for v in views if v["priority"] != "high"]
    auto_rows = db.query("SELECT * FROM drafts WHERE status = 'executed' AND route != 'ask' "
                         "AND acknowledged_at IS NULL ORDER BY executed_at DESC")
    handled = [draft_view(cfg, db, d) for d in auto_rows if d["route"] == "auto_approve"]
    silent = [draft_view(cfg, db, d) for d in auto_rows if d["route"] == "auto_send"]
    in_flight = [draft_view(cfg, db, d) for d in db.query(
        "SELECT * FROM drafts WHERE status IN ('queued', 'executing', 'deferred', 'failed') ORDER BY id")]
    proposals = db.query("SELECT * FROM proposals WHERE status IN ('open', 'info') ORDER BY id DESC")
    for p in proposals:
        p["payload"] = loads(p.pop("payload_json"), {})
    fyi = db.query("SELECT id, source, sender_name, subject, classification_json, received_at FROM events "
                   "WHERE status = 'info' AND (processed_at > ? OR ? IS NULL) ORDER BY received_at DESC LIMIT 20",
                   (last_go or "", last_go))
    for f in fyi:
        f["summary"] = (loads(f.pop("classification_json"), {}) or {}).get("summary") or f["subject"]
        f["received"] = fmt_local(f.pop("received_at"), cfg.profile.timezone)
    # Personal WhatsApp chats are ignored completely: not even counted.
    ignored = db.scalar("SELECT COUNT(*) FROM events WHERE status = 'ignored' AND source != 'whatsapp'") or 0
    opps = db.query("SELECT * FROM opportunities ORDER BY updated_at DESC")
    by_stage = {s: 0 for s in STAGES}
    for o in opps:
        by_stage[o["stage"]] = by_stage.get(o["stage"], 0) + 1
        o["stage_label"] = stage_label(o["stage"])
        o["last_contact"] = ago(max(o.get("last_inbound_at") or "", o.get("last_outbound_at") or "") or None)
    rules = [rule_view(r) for r in db.query("SELECT * FROM rules WHERE status != 'retired' ORDER BY id")]
    last_cycle = db.kv_get("last_cycle", {}) or {}
    deadlines = tasks_mod.deadlines(cfg, db)
    staged = sum(1 for v in views if v["status"] in ("approved", "edited", "rejected"))
    return {
        "generated_for": cfg.profile.name,
        "timezone": cfg.profile.timezone,
        "last_go_at": last_go,
        "last_go_ago": ago(last_go) if last_go else "never",
        "last_cycle": last_cycle,
        "counts": {
            "waiting": len(views),
            "undecided": sum(1 for v in views if v["status"] == "pending"),
            "staged": staged,
            "high_priority": len(high),
            "handled_automatically": len(handled),
            "handled_silently": len(silent),
            "in_flight": len(in_flight),
            **work_queue.go_counts(db),  # what Go runs now, what waits, what failed
            "tasks_open": db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'open'") or 0,
            "tasks_suggested": db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'suggested'") or 0,
            "tasks_due_soon": sum(1 for t in deadlines if t["due_soon"] or t["overdue"]),
            "suggestions": sum(1 for p in proposals if p["status"] == "open"),
            "ignored": ignored,
        },
        "high_priority": high,
        "needs_decision": normal,
        "handled_automatically": handled,
        "handled_silently": silent,
        "in_flight": in_flight,
        "suggestions": proposals,
        "fyi": fyi,
        "upcoming": meetings or [],
        "deadlines": deadlines,
        "pipeline": {"by_stage": by_stage, "opportunities": opps},
        "rules": rules,
        "calibration": calibration(db, cfg),
        "llm": {
            "provider": {"claude-code": "Claude subscription (via Claude Code)", "anthropic": "Claude API",
                         "grok": "Grok", "offline": "offline demo"}.get(cfg.llm.provider, cfg.llm.provider),
            "model": {"claude-code": cfg.llm.claude_code_model, "anthropic": cfg.llm.model,
                      "grok": cfg.llm.grok_model}.get(cfg.llm.provider, "heuristics"),
            "paused": _paused(db),
        },
    }


def _paused(db: Database) -> dict[str, Any] | None:
    pause = db.kv_get("llm_paused")
    until = parse_iso((pause or {}).get("until"))
    return pause if until and until > utcnow() else None
