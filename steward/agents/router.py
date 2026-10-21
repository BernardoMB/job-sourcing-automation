"""Decision Router subagent: may this action run without you, or must it wait?

It is a layered, deterministic judge. It reads the pipeline (Notion via MCP,
cached in SQLite), the decision rules you approved, the calibration learned from
your decision ledger, and the drafter's and reviewer's scores. LLM output can
lower autonomy but can never raise it: only rules you activated grant autonomy.

Hard guardrails always win, in this order:
  offer-stage lockout > sensitive categories > missing CV evidence >
  suspicious message > low confidence > low job-advancement > reviewer escalation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from steward.config import Config
from steward.db import Database, loads
from steward.rules import facts_for, matches
from steward.taxonomy import RISK_ORDER, stage_label


@dataclass
class RouteDecision:
    route: str = "ask"                # ask | auto_approve | auto_send
    band: str = "MID"                 # HIGH | MID | LOW
    priority: str = "normal"          # high | normal
    confidence: float = 0.0
    job_advancement: float = 0.0
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    matched_rule_id: int | None = None
    matched_rule_ids: list[int] = field(default_factory=list)
    shadow_rule_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calibration(db: Database, cfg: Config) -> dict[str, Any]:
    default = {"band_high": cfg.autonomy.band_high, "band_low": cfg.autonomy.band_low, "samples": 0,
               "calibrated": False}
    return {**default, **(db.kv_get("calibration", {}) or {})}


def band_for(confidence: float, cal: dict[str, Any]) -> str:
    if confidence >= cal["band_high"]:
        return "HIGH"
    if confidence < cal["band_low"]:
        return "LOW"
    return "MID"


def route(
    cfg: Config,
    db: Database,
    *,
    event: dict[str, Any],
    cls: dict[str, Any],
    evidence: dict[str, Any],
    sched: dict[str, Any],
    drafted: dict[str, Any],
    review: dict[str, Any],
    opp: dict[str, Any] | None,
) -> RouteDecision:
    auto = cfg.autonomy
    cal = calibration(db, cfg)
    d = RouteDecision()
    cats = set(cls.get("categories", []))

    conf = min(drafted.get("confidence", 0.0), review.get("confidence", 0.0))
    unanswered = list(dict.fromkeys((evidence.get("unanswered") or []) + (drafted.get("unanswered_questions") or [])))
    if unanswered:
        conf = min(conf, 0.5)
    if review.get("verdict") == "escalate":
        conf = min(conf, 0.6)
    elif review.get("verdict") == "revise":
        conf = min(conf, 0.8)
    d.confidence = round(conf, 3)
    d.job_advancement = round(review.get("job_advancement_score", 0.0), 3)
    d.band = band_for(conf, cal)

    high = False
    stage = (opp or {}).get("stage", "")
    if stage == "offer" or cls.get("stage_signal") == "offer" or cats & {"offer", "negotiation"}:
        d.blockers.append("Offer room: autonomy is revoked for this thread; you handle it personally.")
        d.flags.append("offer_lockout")
        high = True
    sensitive = sorted(cats & set(auto.hard_escalate_categories))
    if sensitive:
        d.blockers.append("Sensitive topic: " + ", ".join(s.replace("_", " ") for s in sensitive) + ".")
        high = True
    if unanswered:
        d.blockers.append("Your CV has no evidence for: " + "; ".join(unanswered[:3]))
        d.flags.append("needs_input")
        high = True
    if cls.get("suspicious"):
        d.blockers.append("This message looks suspicious (possible phishing or manipulation).")
        d.flags.append("suspicious")
        high = True
    if d.band == "LOW":
        d.blockers.append(f"Low confidence ({conf:.2f}).")
        high = True
    if "call_notification" in cats:
        d.reasons.append("They called you: replying quickly matters.")
        d.flags.append("call")
        high = True
    if d.job_advancement < auto.job_advancement_flag:
        d.blockers.append(f"This reply may not advance your candidacy (score {d.job_advancement:.2f}). Review carefully.")
        d.flags.append("low_job_advancement")
    if review.get("verdict") == "escalate":
        issues = "; ".join(review.get("issues", [])[:2] + review.get("risks", [])[:1])
        d.blockers.append("Reviewer escalated" + (f": {issues}" if issues else "."))
    if sched.get("kind") in ("unknown", "invite_conflict"):
        d.blockers.append("The calendar situation needs your call.")
    if cls.get("needs_reply") and not drafted.get("reply_body") and event.get("source") != "calendar":
        d.blockers.append("No reply could be drafted.")
    d.priority = "high" if high else "normal"

    facts = facts_for(event, cls, evidence, sched, (opp or {}).get("company", ""))
    rules = db.query("SELECT * FROM rules WHERE status IN ('active', 'shadow') ORDER BY id")
    best: dict[str, Any] | None = None
    for rule in rules:
        ok, why = matches(loads(rule["condition_json"], {}), facts)
        if not ok:
            continue
        if rule["status"] == "shadow":
            d.shadow_rule_ids.append(rule["id"])
            continue
        d.matched_rule_ids.append(rule["id"])
        if best is None or (rule["tier"] == "auto_send" and best["tier"] != "auto_send"):
            best = rule

    if best is None:
        d.reasons.append("No standing rule covers this yet, so it waits for you.")
        if d.shadow_rule_ids:
            d.reasons.append(f"Shadow rule(s) {', '.join(f'#{i}' for i in d.shadow_rule_ids)} are watching how you decide.")
        return d

    d.matched_rule_id = best["id"]
    label = f"rule #{best['id']} '{best['name']}'"
    if d.blockers:
        d.reasons.append(f"Matches {label}, but guardrails require your decision.")
        return d
    threshold = max(best["min_confidence"], cal["band_high"])
    if d.band != "HIGH" or conf < threshold:
        d.reasons.append(f"Matches {label}, but confidence {conf:.2f} is below the {threshold:.2f} needed to act alone.")
        return d
    tier = best["tier"]
    if tier == "auto_send":
        risk = cls.get("risk_level", "medium")
        if RISK_ORDER.get(risk, 1) > RISK_ORDER["low"] or d.job_advancement < auto.job_advancement_auto_send_min:
            tier = "auto_approve"
            d.reasons.append(f"Matches {label}, but silent sending needs low risk and job-advancement >= "
                             f"{auto.job_advancement_auto_send_min}; acting and telling you instead.")
    d.route = tier
    if not d.reasons:
        d.reasons.append(f"Matches {label}: {'act silently' if tier == 'auto_send' else 'act and tell you'}.")
    if stage:
        d.reasons.append(f"Pipeline stage: {stage_label(stage)}.")
    return d
