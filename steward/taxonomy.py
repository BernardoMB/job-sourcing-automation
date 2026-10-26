"""Shared vocabulary: message categories, risk, pipeline stages, autonomy tiers."""

from __future__ import annotations

CATEGORIES: dict[str, str] = {
    "role_pitch": "Recruiter introduces a role or asks whether the candidate is open to it",
    "tech_question": "Asks about specific technologies, tools, or skills",
    "experience_question": "Asks about years of experience, domains, projects, or leadership",
    "availability_request": "Asks when the candidate is free, without proposing a specific time",
    "interview_request": "Proposes one or more specific times for a call or interview",
    "calendar_invite": "A calendar invitation for a meeting",
    "logistics": "Interview logistics, process questions, preparation details",
    "status_update": "Update on the process or next steps; no sensitive question",
    "notice_period": "Asks about start date or notice period",
    "salary_question": "Asks about compensation expectations or current compensation",
    "relocation_question": "Asks about relocation, onsite presence, or location constraints",
    "work_authorization": "Asks about visa, sponsorship, or right to work",
    "offer": "Extends an offer or discusses offer details",
    "negotiation": "Negotiates terms (compensation, equity, start date, title)",
    "references_background": "Requests references, background checks, or personal documents",
    "rejection": "Declines the candidate or closes the position",
    "follow_up": "Job Sourcer-initiated follow-up on a stalled thread",
    "call_notification": "A missed call or voicemail notification (e.g. Google Voice, carrier voicemail-to-email)",
    "meeting_change": "A job-search meeting was moved, cancelled, or declined",
    "other": "Recruiter-related but none of the above",
    "not_recruiter": "Not related to recruiting or the job search",
}

RISK_BY_CATEGORY: dict[str, str] = {
    "role_pitch": "low",
    "tech_question": "low",
    "experience_question": "low",
    "availability_request": "low",
    "logistics": "low",
    "status_update": "low",
    "follow_up": "low",
    "call_notification": "medium",
    "meeting_change": "medium",
    "interview_request": "medium",
    "calendar_invite": "medium",
    "notice_period": "medium",
    "rejection": "medium",
    "other": "medium",
    "not_recruiter": "low",
    "salary_question": "high",
    "relocation_question": "high",
    "work_authorization": "high",
    "offer": "high",
    "negotiation": "high",
    "references_background": "high",
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

QUESTION_TYPES = [
    "tech_experience",
    "years_experience",
    "availability",
    "salary",
    "relocation",
    "work_authorization",
    "notice_period",
    "interest",
    "logistics",
    "other",
]

# Questions answered from the CV (retrieval-based, read-only).
CV_QUESTION_TYPES = {"tech_experience", "years_experience", "other"}

STAGES: dict[str, str] = {
    "new": "New",
    "contacted": "Contacted",
    "screening": "Screening",
    "interviewing": "Interviewing",
    "final": "Final round",
    "offer": "Offer",
    "closed_won": "Accepted",
    "closed_lost": "Closed - lost",
    "withdrawn": "Withdrawn",
}
STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}
CLOSED_STAGES = {"closed_won", "closed_lost", "withdrawn"}
ACTIVE_STAGES = {"new", "contacted", "screening", "interviewing", "final", "offer"}

STAGE_SIGNALS = ["none", "new_opportunity", "screening", "interviewing", "final_round", "offer", "rejection"]
SIGNAL_TO_STAGE = {
    "new_opportunity": "new",
    "screening": "screening",
    "interviewing": "interviewing",
    "final_round": "final",
    "offer": "offer",
    "rejection": "closed_lost",
}

TIERS = ["ask", "auto_approve", "auto_send"]
TIER_LABELS = {
    "ask": "Ask me",
    "auto_approve": "Act and tell me",
    "auto_send": "Act silently",
}

ACTION_TYPES = ["send_email", "send_linkedin", "send_whatsapp", "create_event", "rsvp_event"]

# Things only the candidate can do (they become tasks, never automated actions).
TASK_KINDS = {
    "assessment": "Assessment",
    "form": "Form",
    "document": "Document",
    "application": "Application",
    "prepare": "Preparation",
    "other": "Other",
}


def risk_of(categories: list[str]) -> str:
    level = "low"
    for c in categories:
        r = RISK_BY_CATEGORY.get(c, "medium")
        if RISK_ORDER[r] > RISK_ORDER[level]:
            level = r
    return level


def stage_label(stage: str) -> str:
    return STAGES.get(stage, stage.title())


def advance_stage(current: str, proposed: str) -> str:
    """Stages only move forward automatically; closing is always allowed."""
    if not proposed or proposed == current:
        return current
    if proposed in CLOSED_STAGES:
        return proposed
    if current in CLOSED_STAGES:
        return current
    return proposed if STAGE_ORDER.get(proposed, 0) > STAGE_ORDER.get(current, 0) else current
