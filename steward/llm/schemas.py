"""JSON schemas for every structured LLM call (used with structured outputs)."""

from __future__ import annotations

from typing import Any

from steward.taxonomy import CATEGORIES, QUESTION_TYPES, STAGE_SIGNALS, STAGES, TASK_KINDS


def obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STR = {"type": "string"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
CATEGORY = {"type": "string", "enum": list(CATEGORIES)}

TASK = obj(
    {
        "title": STR,
        "kind": {"type": "string", "enum": list(TASK_KINDS)},
        "due": STR,
        "link": STR,
        "details": STR,
    }
)

CLASSIFY_SCHEMA = obj(
    {
        "is_recruiter_related": BOOL,
        "needs_reply": BOOL,
        "summary": STR,
        "company": STR,
        "role": STR,
        "recruiter_name": STR,
        "phone_number": STR,
        "location": STR,
        "categories": arr(CATEGORY),
        "primary_category": CATEGORY,
        "questions": arr(
            obj(
                {
                    "text": STR,
                    "type": {"type": "string", "enum": QUESTION_TYPES},
                    "entities": arr(STR),
                    "search_terms": arr(STR),
                }
            )
        ),
        "technologies": arr(STR),
        "proposed_times": arr(obj({"start": STR, "end": STR})),
        "meeting_link": STR,
        "stage_signal": {"type": "string", "enum": STAGE_SIGNALS},
        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
        "suspicious": BOOL,
        "candidate_tasks": arr(TASK),
    }
)

DRAFT_SCHEMA = obj(
    {
        "reply_subject": STR,
        "reply_body": STR,
        "answered_questions": arr(STR),
        "unanswered_questions": arr(STR),
        "commitments": arr(STR),
        "next_step": STR,
        "confidence": NUM,
        "rationale": STR,
    }
)

CRITIQUE_SCHEMA = obj(
    {
        "job_advancement_score": NUM,
        "confidence": NUM,
        "verdict": {"type": "string", "enum": ["send", "revise", "escalate"]},
        "issues": arr(STR),
        "risks": arr(STR),
    }
)

DISTILL_SCHEMA = obj(
    {
        "worth_saving": BOOL,
        "preference": STR,
        "categories": arr(CATEGORY),
        "confidence": NUM,
    }
)

INSTRUCT_SCHEMA = obj(
    {
        "reply_needed": BOOL,
        "reply_guidance": STR,
        "meeting_action": {"type": "string",
                           "enum": ["keep", "none", "book", "propose_times", "find_times", "accept_invite",
                                    "decline_invite"]},
        "times": arr(obj({"start": STR, "end": STR})),
        "tasks": arr(TASK),
        "stage": {"type": "string", "enum": ["none", *STAGES]},
        "summary": STR,
    }
)
