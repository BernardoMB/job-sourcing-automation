"""Decision rules: the "don't ask me again" memory, as deterministic predicates.

A rule matches an event only if every category in the event is covered by the
rule, every technology mentioned is in the rule's list (when it has one), and
the channel/sender/company constraints hold. Matching needs no LLM call.
"""

from __future__ import annotations

from typing import Any

from steward.db import Database, dumps, loads
from steward.taxonomy import CATEGORIES, TIER_LABELS
from steward.timeutil import now_iso
from steward.vocab import normalize_entity

SAFE_SCHEDULE_KINDS = {"none", "accept_proposed", "propose_slots", "conflict_counter", "invite_accept"}


def make_condition(
    categories: list[str],
    technologies: list[str] | None = None,
    channels: list[str] | None = None,
    sender: str = "",
    company: str = "",
    require_cv_evidence: bool = True,
) -> dict[str, Any]:
    return {
        "categories": sorted({c for c in categories if c in CATEGORIES and c != "not_recruiter"}),
        "technologies": sorted({normalize_entity(t) for t in technologies or [] if t}),
        "channels": sorted(set(channels or [])),
        "sender": sender.strip().lower(),
        "company": company.strip(),
        "require_cv_evidence": bool(require_cv_evidence),
    }


def facts_for(event: dict[str, Any], cls: dict[str, Any], evidence: dict[str, Any], sched: dict[str, Any],
              company: str = "") -> dict[str, Any]:
    return {
        "categories": {c for c in cls.get("categories", []) if c != "not_recruiter"},
        "technologies": {normalize_entity(t) for t in cls.get("technologies", [])},
        "channel": event.get("source", ""),
        "sender": (event.get("sender_address") or "").lower(),
        "company": company or cls.get("company", ""),
        "cv_all_answered": bool(evidence.get("all_answered", True)),
        "schedule_kind": sched.get("kind", "none"),
    }


def matches(cond: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str]:
    cats = set(cond.get("categories", []))
    if not facts["categories"] or not facts["categories"] <= cats:
        extra = sorted(facts["categories"] - cats)
        return False, f"message also covers {', '.join(extra)}" if extra else "no categories"
    techs = set(cond.get("technologies", []))
    if techs and not facts["technologies"] <= techs:
        return False, "mentions technologies outside the rule"
    if cond.get("channels") and facts["channel"] not in cond["channels"]:
        return False, "different channel"
    sender = cond.get("sender", "")
    if sender:
        if sender.startswith("@"):
            if not facts["sender"].endswith(sender):
                return False, "different sender domain"
        elif facts["sender"] != sender:
            return False, "different sender"
    if cond.get("company") and cond["company"].lower() != (facts["company"] or "").lower():
        return False, "different company"
    if cond.get("require_cv_evidence", True) and not facts["cv_all_answered"]:
        return False, "CV evidence missing"
    if facts["schedule_kind"] not in SAFE_SCHEDULE_KINDS:
        return False, "calendar situation needs a human"
    return True, "matches"


def describe(cond: dict[str, Any]) -> str:
    parts = [" / ".join(c.replace("_", " ") for c in cond.get("categories", [])) or "any message"]
    if cond.get("technologies"):
        parts.append(f"about {', '.join(cond['technologies'])}")
    if cond.get("channels"):
        parts.append(f"on {', '.join(cond['channels'])}")
    if cond.get("sender"):
        parts.append(f"from {cond['sender']}")
    if cond.get("company"):
        parts.append(f"at {cond['company']}")
    if cond.get("require_cv_evidence", True):
        parts.append("when the CV has the evidence")
    return " ".join(parts)


def create_rule(
    db: Database,
    *,
    name: str,
    condition: dict[str, Any],
    tier: str,
    min_confidence: float,
    status: str,
    origin: str,
    created_from_draft_id: int | None = None,
    evidence: Any = None,
) -> int:
    stamp = now_iso()
    return db.insert("rules", {
        "name": name,
        "description": describe(condition),
        "condition_json": dumps(condition),
        "tier": tier,
        "min_confidence": min_confidence,
        "status": status,
        "origin": origin,
        "created_from_draft_id": created_from_draft_id,
        "evidence_json": dumps(evidence) if evidence is not None else None,
        "created_at": stamp,
        "updated_at": stamp,
    })


def rule_view(rule: dict[str, Any]) -> dict[str, Any]:
    cond = loads(rule["condition_json"], {})
    return {
        **{k: v for k, v in rule.items() if k not in ("condition_json", "evidence_json")},
        "condition": cond,
        "tier_label": TIER_LABELS.get(rule["tier"], rule["tier"]),
        "evidence": loads(rule.get("evidence_json"), None),
    }
