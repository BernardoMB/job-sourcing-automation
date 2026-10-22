"""Learning subagent (Stage D): Record -> Mine -> Propose -> (you) Approve -> Apply -> Recalibrate.

It reads the immutable decision ledger and only ever PROPOSES. Nothing it
learns changes live behavior until you accept the proposal in a briefing, and it
can never promote anything to silent execution on its own.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from steward.agents.prompts import DISTILL_SYSTEM
from steward.config import Config
from steward.db import Database, dumps, loads
from steward.ledger import Ledger
from steward.llm import LLM
from steward.llm.schemas import DISTILL_SCHEMA
from steward.rules import describe, make_condition, matches
from steward.taxonomy import TIER_LABELS
from steward.timeutil import days_between, now_iso

CLEAN = 0.98  # similarity at or above this counts as "approved without edits"
TECH_CATEGORIES = {"tech_question", "experience_question"}


def _signature(categories: list[str]) -> str:
    return "+".join(sorted(c for c in categories if c != "not_recruiter"))


class Learner:
    def __init__(self, cfg: Config, db: Database, ledger: Ledger, llm: LLM | None):
        self.cfg, self.db, self.ledger, self.llm = cfg, db, ledger, llm
        self.auto = cfg.autonomy

    async def run(self) -> dict[str, Any]:
        summary = {
            "decayed": self.decay(),
            "calibration": self.calibrate(),
            "rule_proposals": self.mine_rules(),
            "promotion_proposals": self.propose_promotions(),
            "preference_proposals": await self.propose_preferences(),
        }
        return summary

    # -- proposals helpers ------------------------------------------------
    def _propose(self, kind: str, title: str, rationale: str, payload: dict[str, Any], signature: str = "",
                 target_id: int | None = None, status: str = "open") -> int:
        return self.db.insert("proposals", {
            "kind": kind, "signature": signature, "target_id": target_id, "title": title, "rationale": rationale,
            "payload_json": dumps(payload), "status": status, "created_at": now_iso(),
        })

    # -- decay ------------------------------------------------------------------
    def decay(self) -> int:
        count = 0
        for rule in self.db.query("SELECT * FROM rules WHERE status = 'active'"):
            last = rule["last_matched_at"] or rule["updated_at"]
            if days_between(last) >= self.auto.decay_days:
                self.db.update("rules", rule["id"], {"status": "dormant", "updated_at": now_iso()})
                self.ledger.append("rule_change", {"change": "dormant", "reason": f"no match in {self.auto.decay_days} days",
                                                   "rule": rule["name"]}, rule_id=rule["id"])
                self._propose("rule_dormant", f"Rule '{rule['name']}' went dormant",
                              f"It has not matched anything in {self.auto.decay_days} days, so it no longer acts. "
                              "Reactivate it if it still reflects how you decide.",
                              {"rule_id": rule["id"]}, target_id=rule["id"])
                count += 1
        return count

    # -- calibration ------------------------------------------------------------
    def calibrate(self) -> dict[str, Any]:
        samples = []
        for e in self.ledger.decisions():
            p = e["payload"]
            if p.get("confidence") is None or p.get("instructed"):
                continue
            good = p.get("decision") == "approved" or (p.get("decision") == "edited" and p.get("similarity", 0) >= CLEAN)
            samples.append((float(p["confidence"]), good))
        cal = {"band_high": self.auto.band_high, "band_low": self.auto.band_low, "samples": len(samples),
               "calibrated": False, "updated_at": now_iso()}
        if len(samples) >= self.auto.calibration_min_samples:
            chosen = None
            for step in range(60, 100, 2):
                t = step / 100
                above = [g for c, g in samples if c >= t]
                if len(above) >= 5 and sum(above) / len(above) >= self.auto.target_precision:
                    chosen = t
                    break
            # Thresholds come from your own ledger: the HIGH band starts where drafts were reliably
            # approved as-is. If no level is reliable enough, HIGH becomes nearly unreachable.
            cal["band_high"] = max(chosen if chosen is not None else 0.97, self.auto.band_low + 0.1)
            cal["calibrated"] = True
            above = [g for c, g in samples if c >= cal["band_high"]]
            cal["precision_at_high"] = round(sum(above) / len(above), 3) if above else None
        self.db.kv_set("calibration", cal)
        return cal

    # -- rule mining ---------------------------------------------------------------
    def _covered(self, condition: dict[str, Any]) -> bool:
        facts = {"categories": set(condition["categories"]), "technologies": set(condition["technologies"]),
                 "channel": "email", "sender": "", "company": "", "cv_all_answered": True, "schedule_kind": "none"}
        for rule in self.db.query("SELECT * FROM rules WHERE status IN ('active', 'shadow', 'proposed')"):
            if matches(loads(rule["condition_json"], {}), facts)[0]:
                return True
        return False

    def mine_rules(self) -> int:
        hard = set(self.auto.hard_escalate_categories) | {"offer", "negotiation"}
        by_sig: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in self.ledger.decisions():
            p = e["payload"]
            sig = _signature(p.get("categories", []))
            if sig:
                by_sig[sig].append({"seq": e["seq"], "ts": e["ts"], **p})
        created = 0
        for sig, items in by_sig.items():
            cats = sig.split("+")
            if set(cats) & hard or "other" in cats:
                continue
            streak = []
            for item in reversed(items):
                clean = not item.get("instructed") and (item.get("decision") == "approved" or (
                    item.get("decision") == "edited" and item.get("similarity", 0) >= CLEAN))
                if not clean:
                    break
                if item.get("route") in ("auto_approve", "auto_send"):
                    continue
                streak.append(item)
            if len(streak) < self.auto.propose_rule_after:
                continue
            if self.db.query_one("SELECT id FROM proposals WHERE kind='new_rule' AND signature=? AND status='open'", (sig,)):
                continue
            dismissed = self.db.query_one(
                "SELECT created_at FROM proposals WHERE kind='new_rule' AND signature=? AND status='dismissed' "
                "ORDER BY id DESC LIMIT 1", (sig,))
            if dismissed:
                since = [s for s in streak if s["ts"] > dismissed["created_at"]]
                if len(since) < 2 * self.auto.propose_rule_after:
                    continue
            techs: list[str] = []
            if set(cats) & TECH_CATEGORIES:
                techs = sorted({t for s in streak if s.get("cv_all_answered", True) for t in s.get("technologies", [])})
            condition = make_condition(cats, techs, require_cv_evidence=True)
            if self._covered(condition):
                continue
            label = ", ".join(c.replace("_", " ") for c in cats)
            tech_txt = f" about {', '.join(techs)}" if techs else ""
            self._propose(
                "new_rule",
                f"Handle '{label}' messages{tech_txt} for you?",
                f"You approved {len(streak)} drafts of this kind in a row without edits. Job Sourcer would act and tell "
                f"you (\"{TIER_LABELS['auto_approve']}\"), after a shadow period of "
                f"{self.auto.shadow_matches_required} matching decisions. Rule: {describe(condition)}.",
                {"condition": condition, "tier": "auto_approve", "min_confidence": self.auto.default_rule_min_confidence,
                 "evidence_seqs": [s["seq"] for s in streak]},
                signature=sig,
            )
            created += 1
        return created

    # -- promotions -------------------------------------------------------------
    def propose_promotions(self) -> int:
        hard = set(self.auto.hard_escalate_categories)
        created = 0
        for rule in self.db.query("SELECT * FROM rules WHERE status='active' AND tier='auto_approve'"):
            cond = loads(rule["condition_json"], {})
            if set(cond.get("categories", [])) & hard:
                continue
            if rule["times_executed"] < self.auto.propose_promotion_after or rule["overrides"] > 0:
                continue
            if self.db.query_one("SELECT id FROM proposals WHERE kind='promote_rule' AND target_id=? AND status='open'",
                                 (rule["id"],)):
                continue
            self._propose(
                "promote_rule",
                f"Let '{rule['name']}' act silently?",
                f"It has handled {rule['times_executed']} messages and you never flagged one. Silent mode means those "
                "replies go out with no briefing entry (still logged in the ledger). Only you can grant this.",
                {"rule_id": rule["id"], "to_tier": "auto_send"},
                target_id=rule["id"],
            )
            created += 1
        return created

    # -- preferences -------------------------------------------------------------
    async def propose_preferences(self) -> int:
        if self.llm is None:
            return 0
        consumed = set(self.db.kv_get("learner_consumed_seqs", []) or [])
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in self.ledger.decisions():
            if e["seq"] in consumed:
                continue
            p = e["payload"]
            edited = p.get("decision") == "edited" and p.get("similarity", 1) < CLEAN
            rejected_with_note = p.get("decision") == "rejected" and p.get("note")
            instructed = p.get("instructed") and p.get("decision") in ("approved", "edited")
            if edited or rejected_with_note or instructed:
                primary = (p.get("categories") or ["other"])[0]
                groups[primary].append({"seq": e["seq"], **p})
        created = 0
        for primary, items in groups.items():
            if len(items) < self.auto.preference_min_edits:
                continue
            pairs = [{"original": i.get("pre_instruction_reply") or i.get("original_reply", ""),
                      "final": i.get("final_reply", ""),
                      "note": " ".join(x for x in (i.get("instruction", ""), i.get("note", "")) if x),
                      "categories": i.get("categories", [])} for i in items[-6:]]
            user = "Edited or rejected drafts (oldest first):\n\n" + "\n\n".join(
                f"--- Example {n + 1} ({', '.join(p['categories'])})\nORIGINAL DRAFT:\n{p['original']}\n\n"
                f"WHAT THE CANDIDATE SENT INSTEAD:\n{p['final'] or '(rejected)'}\nNOTE: {p['note'] or '-'}"
                for n, p in enumerate(pairs))
            try:
                out = await asyncio.to_thread(self.llm.json, "distill", DISTILL_SYSTEM, user, DISTILL_SCHEMA,
                                              {"pairs": pairs})
            except Exception:  # noqa: BLE001 - learning is best-effort; try again next cycle
                continue
            consumed.update(i["seq"] for i in items)
            if not out.get("worth_saving") or float(out.get("confidence", 0)) < 0.6 or not out.get("preference"):
                continue
            text = str(out["preference"]).strip()
            if self.db.query_one("SELECT id FROM preferences WHERE text = ? AND status != 'retired'", (text,)):
                continue
            self._propose(
                "new_preference",
                f"Save a preference: {text}",
                f"Learned from {len(items)} of your edits/rejections on {primary.replace('_', ' ')} messages. "
                "Accepting adds it to every future draft of this kind.",
                {"text": text, "categories": out.get("categories") or [primary], "evidence_seqs": [i["seq"] for i in items]},
                signature=f"pref:{primary}",
            )
            created += 1
        self.db.kv_set("learner_consumed_seqs", sorted(consumed))
        return created
