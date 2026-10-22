"""Your decisions: approve, edit, reject, "don't ask again", override, accept a suggestion.

Decisions are staged while you go through the briefing and can be changed until
you say "go". At "go" each one is committed to the immutable ledger (the
training data) and the executor runs the approved actions in one batch.
"""

from __future__ import annotations

import difflib
from typing import Any

from steward.config import Config
from steward.db import Database, dumps, loads
from steward.ledger import Ledger
from steward.rules import create_rule, describe, make_condition
from steward.taxonomy import TIER_LABELS
from steward.timeutil import now_iso

CLEAN = 0.98
EDITABLE_FIELDS = {
    "send_email": {"to", "cc", "subject", "body"},
    "send_linkedin": {"text"},
    "create_event": {"title", "start", "end", "description", "attendees"},
    "rsvp_event": {"response"},
}


class DecisionError(ValueError):
    pass


def message_text(actions: list[dict[str, Any]]) -> str:
    for a in actions:
        if a.get("skip"):
            continue
        if a["type"] == "send_email":
            return a["payload"].get("body", "")
        if a["type"] == "send_linkedin":
            return a["payload"].get("text", "")
    return ""


def similarity(a: str, b: str) -> float:
    a, b = " ".join(a.split()), " ".join(b.split())
    if a == b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def _merge_edits(original: list[dict[str, Any]], edited: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(edited) != len(original):
        raise DecisionError("Edited actions must correspond one-to-one with the proposed actions")
    out = []
    for orig, new in zip(original, edited):
        if new.get("type", orig["type"]) != orig["type"]:
            raise DecisionError("You can edit an action or skip it, but not change its type")
        payload = dict(orig["payload"])
        for k, v in (new.get("payload") or {}).items():
            if k in EDITABLE_FIELDS.get(orig["type"], set()):
                if isinstance(v, str) and isinstance(payload.get(k), str):
                    v = v.replace("\r\n", "\n")
                if k in ("to", "cc", "attendees") and isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                payload[k] = v
        merged = {k: v for k, v in orig.items() if k != "skip"}
        merged["payload"] = payload
        if new.get("skip"):
            merged["skip"] = True
        out.append(merged)
    return out


def _require_open(draft: dict[str, Any] | None) -> dict[str, Any]:
    if draft is None:
        raise DecisionError("No such draft")
    if draft["status"] not in ("pending", "approved", "edited", "rejected"):
        raise DecisionError(f"Draft {draft['id']} is already {draft['status']}")
    return draft


def stage(db: Database, draft_id: int, decision: str, edited_actions: list[dict[str, Any]] | None = None,
          note: str = "") -> dict[str, Any]:
    draft = _require_open(db.get_draft(draft_id))
    if decision not in ("approve", "reject"):
        raise DecisionError("decision must be approve or reject")
    if decision == "reject":
        db.update("drafts", draft_id, {"status": "rejected", "decision_note": note or None, "decided_at": now_iso(),
                                       "final_actions_json": None})
        return db.get_draft(draft_id)  # type: ignore[return-value]
    original = loads(draft["actions_json"], [])
    final = _merge_edits(original, edited_actions) if edited_actions else original
    changed = dumps(final) != dumps(original)
    db.update("drafts", draft_id, {
        "status": "edited" if changed else "approved",
        "final_actions_json": dumps(final),
        "decision_note": note or None,
        "decided_at": now_iso(),
    })
    return db.get_draft(draft_id)  # type: ignore[return-value]


def reset(db: Database, draft_id: int) -> None:
    _require_open(db.get_draft(draft_id))
    db.update("drafts", draft_id, {"status": "pending", "final_actions_json": None, "decided_at": None,
                                   "decision_note": None})


def ledger_payload(db: Database, draft: dict[str, Any], decision: str, final: list[dict[str, Any]] | None,
                   note: str = "") -> dict[str, Any]:
    event = db.get_event(draft["event_id"]) or {}
    analysis = loads(draft["analysis_json"], {})
    cls = analysis.get("classification", {})
    original = loads(draft["actions_json"], [])
    orig_text = message_text(original)
    final_text = message_text(final) if final is not None else ""
    return {
        "decision": decision,
        "note": note,
        "channel": draft["channel"],
        "sender_domain": (event.get("sender_address") or "").split("@")[-1],
        "company": cls.get("company", ""),
        "categories": cls.get("categories", []),
        "technologies": cls.get("technologies", []),
        "cv_all_answered": analysis.get("evidence", {}).get("all_answered", True),
        "schedule_kind": analysis.get("schedule", {}).get("kind", "none"),
        "message_excerpt": f"{event.get('subject') or ''}\n{(event.get('body_text') or '')[:1200]}",
        "original_actions": original,
        "final_actions": final,
        "original_reply": orig_text,
        "final_reply": final_text,
        "similarity": similarity(orig_text, final_text) if final is not None else 0.0,
        "confidence": draft["confidence"],
        "job_advancement": draft["job_advancement"],
        "band": draft["routing_band"],
        "route": draft["route"],
        "route_reasons": loads(draft["route_reasons_json"], []),
        "matched_rule_id": draft["matched_rule_id"],
        "shadow_rule_ids": loads(draft["shadow_rule_ids_json"], []),
        # Drafts rewritten from your own instruction teach style and preferences, not autonomy.
        "instructed": bool(analysis.get("instruction")),
        "instruction": analysis.get("instruction", ""),
        "pre_instruction_reply": analysis.get("pre_instruction_reply", ""),
    }


def commit(cfg: Config, db: Database, ledger: Ledger, draft: dict[str, Any]) -> str:
    """Write the staged decision to the ledger. Returns the new draft status."""
    status = draft["status"]
    if status == "rejected":
        payload = ledger_payload(db, draft, "rejected", None, draft.get("decision_note") or "")
        new_status = "dismissed"
    else:
        final = loads(draft["final_actions_json"], None) or loads(draft["actions_json"], [])
        payload = ledger_payload(db, draft, "approved" if status == "approved" else "edited", final,
                                 draft.get("decision_note") or "")
        if payload["decision"] == "edited" and payload["similarity"] >= 1.0:
            payload["decision"] = "approved"  # only non-message fields changed (e.g. skipped a booking)
        new_status = "queued"
    ledger.append("decision", payload, event_id=draft["event_id"], draft_id=draft["id"])
    _evaluate_shadow(cfg, db, ledger, draft, payload)
    db.update("drafts", draft["id"], {"status": new_status})
    if new_status == "dismissed":
        db.update("events", draft["event_id"], {"status": "processed"})
    return new_status


def _evaluate_shadow(cfg: Config, db: Database, ledger: Ledger, draft: dict[str, Any], payload: dict[str, Any]) -> None:
    agree = payload["decision"] == "approved" or (payload["decision"] == "edited" and payload["similarity"] >= CLEAN)
    for rule_id in payload["shadow_rule_ids"]:
        rule = db.get_rule(rule_id)
        if not rule or rule["status"] != "shadow":
            continue
        if agree:
            agreements = rule["shadow_agreements"] + 1
            updates: dict[str, Any] = {"shadow_agreements": agreements, "updated_at": now_iso()}
            if agreements >= cfg.autonomy.shadow_matches_required:
                updates["status"] = "active"
                ledger.append("rule_change", {"change": "graduated", "rule": rule["name"],
                                              "shadow_agreements": agreements}, rule_id=rule_id)
                db.insert("proposals", {
                    "kind": "rule_graduated", "target_id": rule_id, "status": "info", "created_at": now_iso(),
                    "title": f"Rule '{rule['name']}' is now active",
                    "rationale": f"It predicted your decision {agreements} times in a row during its shadow period. "
                                 f"It will now {TIER_LABELS[rule['tier']].lower()}.",
                    "payload_json": dumps({"rule_id": rule_id}),
                })
            db.update("rules", rule_id, updates)
        else:
            db.update("rules", rule_id, {"shadow_agreements": 0,
                                         "shadow_disagreements": rule["shadow_disagreements"] + 1,
                                         "updated_at": now_iso()})
            ledger.append("rule_change", {"change": "shadow_disagreement", "rule": rule["name"],
                                          "your_decision": payload["decision"]}, rule_id=rule_id)


def dont_ask_again(cfg: Config, db: Database, ledger: Ledger, draft_id: int, *, tier: str = "auto_approve",
                   categories: list[str] | None = None, technologies: list[str] | None = None,
                   channels: list[str] | None = None, sender: str = "", company: str = "",
                   min_confidence: float | None = None, name: str = "") -> dict[str, Any]:
    """Create a rule from a draft ("don't ask me again for this kind of decision") and approve the draft."""
    draft = db.get_draft(draft_id)
    if draft is None:
        raise DecisionError("No such draft")
    if tier not in ("auto_approve", "auto_send"):
        raise DecisionError("tier must be auto_approve or auto_send")
    analysis = loads(draft["analysis_json"], {})
    cls = analysis.get("classification", {})
    cats = categories or cls.get("categories", [])
    blocked = sorted(set(cats) & (set(cfg.autonomy.hard_escalate_categories) | {"offer", "negotiation"}))
    if blocked:
        raise DecisionError(
            "Steward always asks you about " + ", ".join(b.replace("_", " ") for b in blocked)
            + ". Remove it from autonomy.hard_escalate_categories in steward.yaml if you really want to delegate it.")
    condition = make_condition(cats, technologies if technologies is not None else [], channels, sender, company,
                               require_cv_evidence=True)
    rule_name = name or (" + ".join(c.replace("_", " ") for c in condition["categories"]).capitalize()
                         + (f" ({', '.join(condition['technologies'])})" if condition["technologies"] else ""))
    rule_id = create_rule(
        db, name=rule_name, condition=condition, tier=tier,
        min_confidence=min_confidence if min_confidence is not None else cfg.autonomy.default_rule_min_confidence,
        status="active", origin="user", created_from_draft_id=draft_id,
    )
    ledger.append("rule_change", {"change": "created", "origin": "user", "rule": rule_name, "tier": tier,
                                  "condition": condition, "from_draft": draft_id}, rule_id=rule_id)
    if draft["status"] == "pending":
        stage(db, draft_id, "approve")
    return {"rule_id": rule_id, "name": rule_name, "description": describe(condition), "tier": tier}


def override(cfg: Config, db: Database, ledger: Ledger, draft_id: int, note: str,
             corrected_reply: str = "") -> dict[str, Any]:
    """Flag an automatic action as wrong. The rule that allowed it is demoted one tier immediately."""
    draft = db.get_draft(draft_id)
    if draft is None or draft["route"] == "ask":
        raise DecisionError("Only actions Steward took on its own can be overridden")
    payload = ledger_payload(db, draft, "override", loads(draft["actions_json"], []), note)
    payload["corrected_reply"] = corrected_reply
    ledger.append("override", payload, event_id=draft["event_id"], draft_id=draft_id, rule_id=draft["matched_rule_id"])
    if corrected_reply:
        # Your correction is a high-signal edit: it trains style and preferences like any other edit.
        edit = dict(payload)
        edit.update({"decision": "edited", "final_reply": corrected_reply,
                     "similarity": similarity(payload["original_reply"], corrected_reply), "override": True})
        ledger.append("decision", edit, event_id=draft["event_id"], draft_id=draft_id)
    demoted = None
    rule = db.get_rule(draft["matched_rule_id"]) if draft["matched_rule_id"] else None
    if rule:
        if rule["tier"] == "auto_send":
            updates = {"tier": "auto_approve"}
            demoted = "auto_approve"
        else:
            updates = {"status": "shadow", "shadow_agreements": 0}
            demoted = "shadow"
        db.update("rules", rule["id"], {**updates, "overrides": rule["overrides"] + 1, "updated_at": now_iso()})
        ledger.append("rule_change", {"change": "demoted", "to": demoted, "reason": "override", "note": note,
                                      "rule": rule["name"]}, rule_id=rule["id"])
        db.insert("proposals", {
            "kind": "review_rule", "target_id": rule["id"], "status": "info", "created_at": now_iso(),
            "title": f"Rule '{rule['name']}' was demoted after your override",
            "rationale": (f"You flagged an action it took. It now {'acts and tells you' if demoted == 'auto_approve' else 'only watches (shadow) until it agrees with you again'}."),
            "payload_json": dumps({"rule_id": rule["id"], "note": note}),
        })
    db.update("drafts", draft_id, {"acknowledged_at": now_iso(), "status_reason": f"Overridden: {note}"[:500]})
    return {"demoted_to": demoted, "rule_id": rule["id"] if rule else None}


def decide_proposal(cfg: Config, db: Database, ledger: Ledger, proposal_id: int, accept: bool) -> dict[str, Any]:
    prop = db.query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if prop is None:
        raise DecisionError("No such proposal")
    if prop["status"] not in ("open", "info"):
        raise DecisionError("This proposal was already decided")
    payload = loads(prop["payload_json"], {})
    result: dict[str, Any] = {"accepted": accept}
    if accept and prop["kind"] == "new_rule":
        status = "shadow" if cfg.autonomy.shadow_matches_required > 0 else "active"
        rule_id = create_rule(db, name=prop["title"].removeprefix("Handle ").rstrip("?").strip("'"),
                              condition=payload["condition"], tier=payload.get("tier", "auto_approve"),
                              min_confidence=payload.get("min_confidence", cfg.autonomy.default_rule_min_confidence),
                              status=status, origin="learner", evidence=payload.get("evidence_seqs"))
        ledger.append("rule_change", {"change": "created", "origin": "learner", "status": status,
                                      "condition": payload["condition"], "proposal": proposal_id}, rule_id=rule_id)
        result["rule_id"] = rule_id
    elif accept and prop["kind"] == "promote_rule":
        rule = db.get_rule(payload["rule_id"])
        if rule and rule["status"] == "active":
            db.update("rules", rule["id"], {"tier": "auto_send", "updated_at": now_iso()})
            ledger.append("rule_change", {"change": "promoted", "to": "auto_send", "by": "you"}, rule_id=rule["id"])
    elif accept and prop["kind"] == "new_preference":
        pref_id = db.insert("preferences", {"text": payload["text"], "categories_json": dumps(payload.get("categories", [])),
                                            "status": "active", "origin": "learner",
                                            "evidence_json": dumps(payload.get("evidence_seqs", [])),
                                            "created_at": now_iso(), "updated_at": now_iso()})
        ledger.append("preference_change", {"change": "created", "text": payload["text"], "proposal": proposal_id})
        result["preference_id"] = pref_id
    elif accept and prop["kind"] == "rule_dormant":
        db.update("rules", payload["rule_id"], {"status": "active", "last_matched_at": now_iso(), "updated_at": now_iso()})
        ledger.append("rule_change", {"change": "reactivated", "by": "you"}, rule_id=payload["rule_id"])
    db.update("proposals", proposal_id, {"status": "accepted" if accept else "dismissed", "decided_at": now_iso()})
    if prop["status"] == "open":
        ledger.append("proposal", {"kind": prop["kind"], "title": prop["title"], "accepted": accept},
                      rule_id=payload.get("rule_id"))
    return result


def rule_action(db: Database, ledger: Ledger, rule_id: int, action: str) -> None:
    rule = db.get_rule(rule_id)
    if rule is None:
        raise DecisionError("No such rule")
    updates: dict[str, Any]
    if action == "retire":
        updates = {"status": "retired"}
    elif action == "reactivate":
        updates = {"status": "active", "last_matched_at": now_iso()}
    elif action == "demote":
        updates = {"tier": "auto_approve"} if rule["tier"] == "auto_send" else {"status": "shadow", "shadow_agreements": 0}
    elif action == "promote":
        if rule["status"] == "shadow":
            updates = {"status": "active"}
        else:
            updates = {"tier": "auto_send"}
    else:
        raise DecisionError("action must be retire, reactivate, demote, or promote")
    db.update("rules", rule_id, {**updates, "updated_at": now_iso()})
    ledger.append("rule_change", {"change": action, "by": "you", **updates}, rule_id=rule_id)


def add_preference(db: Database, ledger: Ledger, text: str, categories: list[str] | None = None,
                   company: str = "", sender: str = "", draft_id: int | None = None) -> int:
    text = text.strip()
    if not text:
        raise DecisionError("Write the rule first")
    pref_id = db.insert("preferences", {"text": text, "categories_json": dumps(categories or []),
                                        "company": company.strip() or None, "sender": sender.strip().lower() or None,
                                        "status": "active", "origin": "user", "created_at": now_iso(),
                                        "updated_at": now_iso()})
    ledger.append("preference_change", {"change": "created", "origin": "user", "text": text,
                                        "categories": categories or [], "company": company, "sender": sender},
                  draft_id=draft_id)
    return pref_id


RULE_SCOPES = {
    "kind": "messages like this one",
    "company": "this company",
    "sender": "this recruiter",
    "all": "every message",
}


def teach_rule(db: Database, ledger: Ledger, draft_id: int, text: str, scope: str = "kind") -> dict[str, Any]:
    """A standing instruction written on a briefing item: shapes every future draft in its scope."""
    draft = db.get_draft(draft_id)
    if draft is None:
        raise DecisionError("No such draft")
    if scope not in RULE_SCOPES:
        raise DecisionError("scope must be kind, company, sender, or all")
    cls = (loads(draft["analysis_json"], {}) or {}).get("classification", {}) or {}
    event = db.get_event(draft["event_id"]) or {}
    opp = db.get_opportunity(draft["opportunity_id"]) or {}
    categories = [c for c in cls.get("categories", []) if c != "not_recruiter"] if scope == "kind" else []
    company = (opp.get("company") or cls.get("company") or "") if scope == "company" else ""
    sender = (event.get("sender_address") or "") if scope == "sender" else ""
    if scope == "company" and not company:
        raise DecisionError("Steward doesn't know this item's company; choose another scope")
    if scope == "sender" and not sender:
        raise DecisionError("Steward doesn't know who sent this item; choose another scope")
    pref_id = add_preference(db, ledger, text, categories, company, sender, draft_id=draft_id)
    where = {"kind": " / ".join(c.replace("_", " ") for c in categories) or "messages like this one",
             "company": company, "sender": sender, "all": "every message"}[scope]
    return {"preference_id": pref_id, "applies_to": where}


def retire_preference(db: Database, ledger: Ledger, pref_id: int) -> None:
    pref = db.query_one("SELECT * FROM preferences WHERE id = ?", (pref_id,))
    if pref is None:
        raise DecisionError("No such preference")
    db.update("preferences", pref_id, {"status": "retired", "updated_at": now_iso()})
    ledger.append("preference_change", {"change": "retired", "text": pref["text"]})


def active_preferences(db: Database, categories: list[str], company: str = "", sender: str = "") -> list[str]:
    """Standing instructions that apply to a message: by kind, company, recruiter, or everything."""
    out = []
    for p in db.query("SELECT * FROM preferences WHERE status = 'active' ORDER BY id"):
        cats = loads(p["categories_json"], [])
        if cats and not set(cats) & set(categories):
            continue
        if p.get("company") and p["company"].lower() != (company or "").lower():
            continue
        if p.get("sender") and p["sender"] != (sender or "").lower():
            continue
        out.append(p["text"])
    return out
