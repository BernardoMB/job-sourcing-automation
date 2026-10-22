"""Drafting subagent: writes the reply in your voice, optimized for landing the job."""

from __future__ import annotations

from typing import Any

from steward.agents.base import AgentContext, block, call_llm, clamp
from steward.agents.classifier import render_thread
from steward.agents.prompts import CRITIQUE_SYSTEM, DRAFT_SYSTEM
from steward.llm.schemas import CRITIQUE_SCHEMA, DRAFT_SCHEMA
from steward.taxonomy import stage_label


def profile_block(ctx: AgentContext) -> dict[str, Any]:
    p = ctx.cfg.profile
    return {
        "name": p.name,
        "headline": p.headline,
        "target_roles": p.target_roles,
        "preferred_locations": p.preferred_locations,
        "remote_preference": p.remote_preference,
        "work_authorization": p.work_authorization,
        "notice_period": p.notice_period,
        "relocation": p.relocation,
        "salary_policy": p.salary_policy,
        "facts": p.facts,
        "signature": p.signature or p.name,
        "linkedin_url": p.linkedin_url,
    }


def schedule_block(sched: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": sched.get("kind"),
        "meaning": {
            "none": "No scheduling needed.",
            "accept_proposed": "A time the recruiter proposed is free. Confirm it.",
            "conflict_counter": "None of the proposed times work. Politely decline them and offer the slots.",
            "propose_slots": "Offer these slots for a call.",
            "invite_accept": "The invitation fits the calendar and will be accepted.",
            "invite_conflict": "The invitation conflicts. Ask to move it and offer the slots.",
            "invite_added": "The recruiter's invitation is already on the candidate's calendar at accepted_time. "
                            "Confirm that time if a reply is needed; never propose or book another one.",
            "unknown": "Availability is unknown. Do not commit to any time.",
        }.get(sched.get("kind", "none"), ""),
        "accepted_time": sched.get("accepted_label", ""),
        "calendar_invite_will_be_sent": bool(sched.get("booking")),
        "slots": [s["label"] for s in sched.get("slots", [])],
        "checked": [{"time": c.get("label"), "ok": c.get("ok", c.get("free")), "problems": c.get("problems", [])}
                    for c in sched.get("checks", [])],
    }


def opportunity_block(opp: dict[str, Any] | None) -> dict[str, Any]:
    if not opp:
        return {"known": False}
    return {"known": True, "company": opp.get("company"), "role": opp.get("role"),
            "stage": stage_label(opp.get("stage", "new")), "notes": opp.get("notes") or ""}


async def draft(
    ctx: AgentContext,
    event: dict[str, Any],
    thread: list[dict[str, Any]],
    cls: dict[str, Any],
    evidence: dict[str, Any],
    sched: dict[str, Any],
    opp: dict[str, Any] | None,
    preferences: list[str],
    examples: list[dict[str, Any]],
    instruction: str = "",
) -> dict[str, Any]:
    user = "\n\n".join([
        block("channel", event["source"]),
        block("candidate_profile", profile_block(ctx)),
        block("opportunity", opportunity_block(opp)),
        block("thread_history", render_thread(thread)),
        block("message", f"From: {event.get('sender_name')} <{event.get('sender_address')}>\n"
                         f"Subject: {event.get('subject') or ''}\n\n{event.get('body_text') or ''}"),
        block("classification", {k: cls.get(k) for k in ("summary", "categories", "questions", "company", "role",
                                                         "candidate_tasks")}),
        block("cv_evidence", {"answers": evidence.get("items", []), "fit": evidence.get("fit", [])}),
        block("schedule", schedule_block(sched)),
        block("preferences", preferences or ["(none yet)"]),
        block("examples", examples or ["(no similar past decisions yet)"]),
        *([block("candidate_instruction", instruction)] if instruction else []),
    ])
    context = {"event": event, "classification": cls, "evidence": evidence, "schedule": sched,
               "opportunity": opp, "preferences": preferences, "examples": examples, "instruction": instruction}
    out = await call_llm(ctx, "draft", "drafter", event.get("id"), DRAFT_SYSTEM, user, DRAFT_SCHEMA, context)
    out["confidence"] = clamp(out.get("confidence"), default=0.5)
    for key in ("answered_questions", "unanswered_questions", "commitments"):
        out[key] = [str(x) for x in out.get(key, []) or []]
    out["reply_body"] = str(out.get("reply_body", "")).strip()
    out["reply_subject"] = str(out.get("reply_subject", "")).strip()
    return out


async def critique(
    ctx: AgentContext,
    event: dict[str, Any],
    cls: dict[str, Any],
    drafted: dict[str, Any],
    evidence: dict[str, Any],
    sched: dict[str, Any],
    opp: dict[str, Any] | None,
) -> dict[str, Any]:
    user = "\n\n".join([
        block("message", f"From: {event.get('sender_name')}\nSubject: {event.get('subject') or ''}\n\n"
                         f"{event.get('body_text') or ''}"),
        block("classification", {k: cls[k] for k in ("categories", "questions", "stage_signal")}),
        block("opportunity", opportunity_block(opp)),
        block("candidate_profile", profile_block(ctx)),
        block("cv_evidence", {"answers": evidence.get("items", []), "fit": evidence.get("fit", [])}),
        block("schedule", schedule_block(sched)),
        block("draft", {"subject": drafted.get("reply_subject"), "body": drafted.get("reply_body"),
                        "unanswered_questions": drafted.get("unanswered_questions"),
                        "commitments": drafted.get("commitments")}),
    ])
    context = {"event": event, "classification": cls, "draft": drafted, "evidence": evidence, "schedule": sched}
    out = await call_llm(ctx, "critique", "critic", event.get("id"), CRITIQUE_SYSTEM, user, CRITIQUE_SCHEMA, context)
    out["job_advancement_score"] = clamp(out.get("job_advancement_score"), default=0.5)
    out["confidence"] = clamp(out.get("confidence"), default=0.5)
    if out.get("verdict") not in ("send", "revise", "escalate"):
        out["verdict"] = "escalate"
    out["issues"] = [str(x) for x in out.get("issues", []) or []]
    out["risks"] = [str(x) for x in out.get("risks", []) or []]
    return out
