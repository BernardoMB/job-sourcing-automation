"""The orchestrator: the walk-away loop.

  Phase 1  Silent accumulation   watchers pull new events through the MCP servers
  Phase 2  Subagent pipeline     classify -> CV evidence -> schedule -> draft -> review -> route
  Phase 3  Auto-execution        only what an activated rule allows, executed immediately
  Phase 4  Briefing              everything else waits in the queue for you
  Phase 5  Batch execution       when you say "go" (see executor.run_batch)
  Stage D  Learning              mine the ledger, propose rules and preferences
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from steward import (activity, assessments, channels, decisions, linkedin_bridge, notify, pipeline, priority,
                     reconcile, sound)
from steward import tasks as tasks_mod
from steward.agents import classifier, cv_analyst, drafter, fewshot, instructor, scheduler
from steward.agents.base import AgentContext
from steward.agents.learner import Learner
from steward.agents.router import RouteDecision, route
from steward.config import Config
from steward.db import Database, dumps, loads
from steward.executor import Executor
from steward.ledger import Ledger
from steward.llm import LLMError, LLMRefusal, LLMUnavailable, get_llm
from steward.mcp_hub import IntegrationDisabled, MCPHub, MCPToolError
from steward.taxonomy import ACTIVE_STAGES, risk_of
from steward.timeutil import days_between, fmt_local, fmt_range, now_iso, parse_iso, to_iso, utcnow
from steward.work_queue import queue_actions

log = logging.getLogger("steward")

PREFILTER = re.compile(
    r"\b(recruit|hiring|hire|role|position|opportunit|job|interview|candidate|talent|offer|salary|compensation|"
    r"resume|cv|your (background|profile|experience)|engineer|developer|team|headcount|availability|schedule|"
    r"voicemail|missed call|assessment|coding (test|challenge)|online test|hackerrank|codility|codesignal|"
    r"testgorilla|hackerearth|coderbyte|karat|mettl|imocha|testdome|devskiller|coderpad|codingame|hirevue)",
    re.IGNORECASE,
)


def hydrate(event: dict[str, Any]) -> dict[str, Any]:
    event = dict(event)
    event["raw"] = loads(event.get("raw_json"), {}) or {}
    return event


class Steward:
    def __init__(self, cfg: Config, db: Database | None = None, llm=None):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.ledger = Ledger(self.db)
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm(self.cfg)
        return self._llm

    def ctx(self, hub: MCPHub) -> AgentContext:
        return AgentContext(self.cfg, self.db, self.llm, hub, self.ledger)

    # -- the cycle --------------------------------------------------------------
    async def cycle(self, force: bool = False, trigger: str = "scheduled") -> dict[str, Any]:
        started = utcnow()
        summary: dict[str, Any] = {"ingested": {}, "processed": 0, "auto_executed": 0, "errors": []}
        checked: dict[str, str] = {}
        bridge = await asyncio.to_thread(linkedin_bridge.ensure_running, self.cfg)
        if bridge.get("note"):
            summary["errors"].append(f"linkedin bridge: {bridge['note']}")
        self.retire_linkedin_email_drafts()
        last_event = self.db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0
        async with MCPHub(self.cfg) as hub:
            for source, fn, minutes in (
                ("email", self.ingest_email, self.cfg.mail.poll_minutes),
                ("linkedin", self.ingest_linkedin, self.cfg.linkedin.poll_minutes),
                ("calendar", self.ingest_calendar, self.cfg.calendar.poll_minutes),
            ):
                # A LinkedIn message notification in the mail makes LinkedIn due right away.
                nudged = source == "linkedin" and bool(self.db.kv_get("linkedin:check_now"))
                if not force and not nudged and not self._due(source, minutes):
                    checked[source] = "not due"
                    continue
                try:
                    summary["ingested"][source] = await fn(hub)
                    self.db.kv_set(f"last_poll:{source}", now_iso())
                    checked[source] = f"{summary['ingested'][source]} new" + (" (a LinkedIn email said someone "
                                                                              "messaged you)" if nudged else "")
                    if nudged:
                        self.db.kv_set("linkedin:check_now", None)
                except IntegrationDisabled:
                    checked[source] = "off"
                    continue
                except (MCPToolError, OSError) as exc:
                    checked[source] = "failed"
                    summary["errors"].append(f"{source}: {exc}")
                    log.warning("ingest %s failed: %s", source, exc)
            # Chime now, before the slow drafting: filtered-out mail and our own follow-ups don't count.
            summary["new_events"] = self.db.scalar(
                "SELECT COUNT(*) FROM events WHERE id > ? AND source != 'steward' AND status != 'ignored'",
                (last_event,)) or 0
            sound.new_events(self.cfg, summary["new_events"])
            # Anything you already handled yourself (on LinkedIn, by email, in your calendar) is closed now.
            summary["reconciled"] = await reconcile.run(self, hub, checked, summary["errors"])
            summary["ingested"]["follow_ups"] = self.detect_follow_ups()
            try:
                summary["reprioritized"] = await self.reprioritize(hub)
            except (MCPToolError, OSError) as exc:
                summary["errors"].append(f"reprioritize: {exc}")
            processed, auto = await self.process_pending(hub, summary["errors"])
            summary["processed"], summary["auto_executed"] = processed, auto
            self.rescore()
            executor = Executor(self.cfg, self.db, self.ledger, hub)
            due = await executor.run_queue(include_deferred_only=True)
            summary["deferred_retried"] = due["executed"]
            try:
                summary["learning"] = await Learner(self.cfg, self.db, self.ledger, self.llm).run()
            except Exception as exc:  # noqa: BLE001 - learning never blocks the loop
                summary["errors"].append(f"learning: {exc}")
            await self.sync_notion(hub, summary["errors"])
            try:
                summary["notified"] = await notify.maybe_notify(self.cfg, self.db, hub)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"notify: {exc}")
        self.db.kv_set("last_cycle", {"at": now_iso(), "summary": {k: v for k, v in summary.items() if k != "learning"}})
        self.db.kv_set("last_check_errors", summary["errors"])
        self._log_check(trigger, checked, summary, (utcnow() - started).total_seconds())
        return summary

    def _log_check(self, trigger: str, checked: dict[str, str], summary: dict[str, Any], seconds: float) -> None:
        names = {"email": "email", "linkedin": "LinkedIn", "calendar": "calendar"}
        idle = all(state in ("not due", "off") for state in checked.values()) and not (
            summary["processed"] or summary["auto_executed"] or summary.get("deferred_retried") or summary["errors"]
            or summary.get("reconciled"))
        if idle and trigger == "scheduled":
            return  # the loop woke up but nothing was due: not a check worth a line
        parts = [f"{names[s]}: {state}" for s, state in checked.items()]
        for label, n in (("processed", summary["processed"]), ("handled automatically", summary["auto_executed"]),
                         ("reconciled", summary.get("reconciled", 0)),
                         ("deferred actions sent", summary.get("deferred_retried", 0))):
            if n:
                parts.append(f"{label} {n}")
        errors = summary["errors"]
        activity.record(self.db, "check", f"Check ({trigger}, {seconds:.0f}s): " + " · ".join(parts)
                        + (f" · {len(errors)} problem{'s' if len(errors) != 1 else ''}" if errors else ""),
                        "warn" if errors else "info", details={"checked": checked, "errors": errors})
        for err in errors:
            activity.record(self.db, "check", str(err)[:1000], "error")

    def retire_linkedin_email_drafts(self) -> int:
        """Drafts that would email a LinkedIn address (made before that was forbidden) are withdrawn."""
        retired = 0
        for d in self.db.query("SELECT * FROM drafts WHERE status IN ('pending', 'approved', 'edited', 'queued', "
                               "'deferred', 'failed')"):
            actions = loads(d["final_actions_json"], None) or loads(d["actions_json"], [])
            if any(a["type"] == "send_email" and not a.get("skip")
                   and any(channels.is_linkedin_address(t) for t in a["payload"].get("to", [])) for a in actions):
                why = "Withdrawn: it would email LinkedIn. LinkedIn messages are answered on LinkedIn."
                self.db.update("drafts", d["id"], {"status": "superseded", "status_reason": why})
                activity.record(self.db, "draft", f"{d['summary']}: {why}", "warn", draft_id=d["id"])
                retired += 1
        return retired

    async def go(self) -> dict[str, Any]:
        async with MCPHub(self.cfg) as hub:
            result = await Executor(self.cfg, self.db, self.ledger, hub).run_batch()
            await self.sync_notion(hub, result.setdefault("errors", []))
            # Superseded drafts (calendar changed) get re-drafted right away.
            processed, auto = await self.process_pending(hub, result["errors"])
            result["redrafted"] = processed
            self.rescore()
        try:
            result["learning"] = await Learner(self.cfg, self.db, self.ledger, self.llm).run()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"learning: {exc}")
        return result

    async def execute_now(self, draft_id: int) -> dict[str, Any]:
        """Re-execute (and the Execute button): run a queued item now, skipping LinkedIn business hours and pacing.

        Pending confirmation (approved, not yet Go): committed and run. Deferred or failed: run again. Handed
        to the linkedin-toolkit: sent from its queue if it is still waiting there (Autopilot), sent again if
        the toolkit failed it, and left alone if the toolkit already sent it."""
        draft = self.db.get_draft(draft_id)
        if draft is None or not queue_actions(self.db, draft)["can_reexecute"]:
            raise decisions.DecisionError("This item cannot be re-executed (it is done, discarded, or not decided yet)")
        async with MCPHub(self.cfg) as hub:
            if draft["status"] in ("approved", "edited"):
                decisions.commit(self.cfg, self.db, self.ledger, draft)
            for x in reconcile.handed_off(self):
                if x["draft_id"] != draft_id:
                    continue
                items = await reconcile.toolkit_items(hub) or {}
                state = (items.get(x["queue_id"]) or {}).get("status", "")
                if state == "sent":
                    await reconcile.sync_toolkit(self, hub)
                    return {"draft_id": draft_id, "status": "executed", "reason": "The LinkedIn toolkit already sent it."}
                if state in ("pending", "approved"):
                    problem = await reconcile.toolkit_decide(hub, x["queue_id"], approve=True)
                    reason = ("Sent from the LinkedIn toolkit queue." if not problem else
                              "It is waiting in the LinkedIn toolkit, which is in Copilot mode: only you can approve "
                              "it, in the extension popup (or turn on Autopilot there).")
                    activity.record(self.db, "action", f"{draft['summary']}: {reason}", "warn" if problem else "info",
                                    draft_id=draft_id)
                    return {"draft_id": draft_id, "status": "executed" if not problem else "waiting", "reason": reason}
                # failed, rejected, or gone from the toolkit: send it again
                self.db.update("executions", x["id"], {"status": "failed", "updated_at": now_iso(),
                                                       "error": f"re-executed by you (toolkit status: {state or 'gone'})"})
            # An interrupted attempt is retried because you said so (the Queue tab asks you to check first).
            self.db.execute("UPDATE executions SET status = 'failed', updated_at = ? WHERE draft_id = ? AND status = "
                            "'unknown'", (now_iso(), draft_id))
            result = await Executor(self.cfg, self.db, self.ledger, hub).execute_draft(draft_id, force=True)
            await self.sync_notion(hub, [])
        return result

    async def discard(self, draft_id: int) -> dict[str, Any]:
        """Discard a queued item: it will not run. One waiting in the linkedin-toolkit is rejected there too."""
        draft = self.db.get_draft(draft_id)
        if draft is None or not queue_actions(self.db, draft)["can_discard"]:
            raise decisions.DecisionError("This item cannot be discarded (it is done, running, or already closed)")
        reason = "Discarded by you."
        waiting = [x for x in reconcile.handed_off(self) if x["draft_id"] == draft_id]
        if waiting:
            async with MCPHub(self.cfg) as hub:
                items = await reconcile.toolkit_items(hub) or {}
                for x in waiting:
                    if (items.get(x["queue_id"]) or {}).get("status") in ("pending", "approved") and                             await reconcile.toolkit_decide(hub, x["queue_id"], approve=False):
                        reason += (" It is still waiting in the LinkedIn toolkit (Copilot mode): reject it in the "
                                   "extension popup too, or it may still be sent.")
        reconcile.close(self, draft, reason, status="discarded", level="warn", kind="discard")
        return {"draft_id": draft_id, "status": "discarded", "reason": reason}

    async def go_one(self, draft_id: int) -> dict[str, Any]:
        """Commit and execute a single decision right away (the "act on each approval" setting)."""
        draft = self.db.get_draft(draft_id)
        if draft is None or draft["status"] not in ("approved", "edited", "rejected"):
            return {"draft_id": draft_id, "status": "nothing to do"}
        status = decisions.commit(self.cfg, self.db, self.ledger, draft)
        if status != "queued":
            return {"draft_id": draft_id, "status": status}
        async with MCPHub(self.cfg) as hub:
            result = await Executor(self.cfg, self.db, self.ledger, hub).execute_draft(draft_id)
            errors: list[str] = []
            await self.sync_notion(hub, errors)
            if result["status"] == "superseded":
                await self.process_pending(hub, errors)
            self.rescore()
        return result

    # -- your instructions and rules ---------------------------------------------
    async def revise(self, draft_id: int, instruction: str = "", reason: str = "") -> dict[str, Any]:
        """Rebuild a waiting draft: from your instruction ("decline politely...") or with a rule you just added.

        The new version replaces the old one in the briefing and waits for your approval.
        """
        old = self.db.get_draft(draft_id)
        if old is None or old["status"] not in ("pending", "approved", "edited", "rejected"):
            raise decisions.DecisionError("Only items still waiting for you can be revised")
        instruction = (instruction or "").strip()
        event = hydrate(self.db.get_event(old["event_id"]) or {})
        analysis = loads(old["analysis_json"], {}) or {}
        old_actions = loads(old["actions_json"], []) or []
        opp = self.db.get_opportunity(old["opportunity_id"])
        warnings: list[str] = []
        async with MCPHub(self.cfg) as hub:
            ctx = self.ctx(hub)
            cls = analysis.get("classification") or {}
            if "questions" not in cls:  # an item Job Sourcer could not triage before
                cls = await classifier.classify(ctx, event, self.thread_history(event))
            evidence = analysis.get("evidence") or await cv_analyst.analyze(ctx, cls)
            sched = analysis.get("schedule") or {"kind": "none", "checks": [], "slots": [], "booking": None,
                                                  "rsvp": None, "notes": []}
            plan: dict[str, Any] | None = None
            reply_needed = bool(decisions.message_text(old_actions)) or bool(cls.get("needs_reply"))
            if instruction:
                plan = await instructor.plan(ctx, event, cls, old_actions, instruction)
                sched = await self._schedule_from_plan(ctx, event, cls, plan, sched, warnings)
                reply_needed = plan["reply_needed"]
                for t in plan["tasks"]:
                    tasks_mod.add(self.cfg, self.db, self.ledger, t["title"], details=t["details"], kind=t["kind"],
                                  link=t["link"], due_at=t["due"] or None, opportunity_id=opp["id"] if opp else None,
                                  origin="you", event_id=event.get("id"))
                if plan["stage"] != "none" and opp:
                    pipeline.set_stage(self.db, opp["id"], plan["stage"], force=True)
            if "call_notification" in cls.get("categories", []) and not event["raw"].get("reply_via"):
                via = self._reply_target(opp)
                if via:
                    event["raw"]["reply_via"] = via
            prefs = decisions.active_preferences(self.db, cls.get("categories", []),
                                                 (opp or {}).get("company") or cls.get("company", ""),
                                                 event.get("sender_address", ""))
            if reply_needed:
                guidance = instruction + (f"\nPlan: {plan['reply_guidance']}" if plan and plan["reply_guidance"] else "")
                drafted = await drafter.draft(ctx, event, self.thread_history(event), cls, evidence, sched, opp, prefs,
                                              fewshot.retrieve(self.ledger, event, cls), instruction=guidance)
            else:
                drafted = {"reply_subject": "", "reply_body": "", "answered_questions": [],
                           "unanswered_questions": [], "commitments": [], "next_step": "", "confidence": 0.95,
                           "rationale": "You asked for no reply."}
            actions = self.build_actions(event, cls, drafted, sched, opp)
        if instruction:
            reasons = [f"Rewritten from your instruction: “{instruction}”"]
            if plan and plan["summary"]:
                reasons.append(plan["summary"])
        else:
            reasons = [reason or "Re-drafted with your new rule."]
        if not actions:
            warnings.append("Nothing to send: approve to close this item, or write another instruction.")
        new_analysis = {**analysis, "classification": cls, "evidence": evidence, "schedule": sched, "draft": drafted,
                        "preferences_used": prefs, "revised_from": draft_id,
                        "instruction": instruction or analysis.get("instruction", ""),
                        "pre_instruction_reply": analysis.get("pre_instruction_reply")
                        or decisions.message_text(old_actions),
                        "plan": plan}
        new_id = self.db.insert("drafts", {
            "event_id": old["event_id"], "opportunity_id": old["opportunity_id"], "channel": old["channel"],
            "summary": old["summary"], "actions_json": dumps(actions), "analysis_json": dumps(new_analysis),
            "confidence": drafted.get("confidence", old["confidence"]), "job_advancement": old["job_advancement"],
            "routing_band": old["routing_band"], "risk_level": old["risk_level"], "priority": old["priority"],
            "route": "ask", "route_reasons_json": dumps(warnings + reasons), "matched_rule_id": None,
            "shadow_rule_ids_json": "[]", "status": "pending", "created_at": now_iso(), "revised_from": draft_id,
        })
        self.db.update("drafts", draft_id, {"status": "superseded",
                                            "status_reason": f"Replaced by a revised version (#{new_id})."})
        self.db.execute("UPDATE tasks SET draft_id = ? WHERE draft_id = ?", (new_id, draft_id))
        if instruction:
            self.ledger.append("instruction", {"instruction": instruction, "plan": plan, "replaced_draft": draft_id,
                                               "new_draft": new_id, "warnings": warnings},
                               event_id=old["event_id"], draft_id=new_id)
        self.rescore()
        return {"draft_id": new_id, "summary": (plan or {}).get("summary") or reasons[0], "warnings": warnings}

    async def _schedule_from_plan(self, ctx: AgentContext, event: dict[str, Any], cls: dict[str, Any],
                                  plan: dict[str, Any], sched: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        action = plan["meeting_action"]
        if action == "keep":
            return sched
        empty = {"kind": "none", "checks": [], "slots": [], "booking": None, "rsvp": None, "notes": []}
        sch = self.cfg.scheduling
        duration = timedelta(minutes=sch.default_duration_minutes)

        def window(t: dict[str, str]) -> tuple[str, str]:
            start = parse_iso(t["start"])
            end = parse_iso(t["end"]) if t.get("end") else None
            return to_iso(start), to_iso(end if end and start and end > start else start + duration)

        if action == "none":
            return empty
        raw = event.get("raw", {})
        if action in ("accept_invite", "decline_invite"):
            if event.get("source") != "calendar" or not raw.get("id"):
                warnings.append("There is no calendar invitation on this item to answer.")
                return empty
            response = "accepted" if action == "accept_invite" else "declined"
            return {**empty, "kind": "invite_accept" if response == "accepted" else "none",
                    "rsvp": {"event_id": raw["id"], "response": response}}
        if action == "find_times" or (action in ("book", "propose_times") and not plan["times"]):
            now = utcnow()
            return {**empty, "kind": "propose_slots",
                    "slots": await scheduler.slots_between(ctx, now, now + timedelta(days=sch.lookahead_days),
                                                           sch.default_duration_minutes)}
        times = [window(t) for t in plan["times"]]
        if action == "propose_times":
            return {**empty, "kind": "propose_slots",
                    "slots": [{"start": s, "end": e, "label": scheduler.label(ctx, s, e)} for s, e in times]}
        start, end = times[0]
        label = scheduler.label(ctx, start, end)
        check = await ctx.hub.call("calendar", "check_availability",
                                   {"start": start, "end": end, "buffer_minutes": sch.buffer_minutes})
        if not check.get("free"):
            titles = ", ".join(c.get("title", "busy") for c in check.get("conflicts", []))
            warnings.append(f"{label} conflicts with {titles or 'another event'}, so it is offered as a proposal "
                            "instead of being booked.")
            return {**empty, "kind": "propose_slots", "slots": [{"start": start, "end": end, "label": label}]}
        booking = None
        if event.get("sender_address") and "@" in event.get("sender_address", ""):
            booking = {"title": scheduler.meeting_title(ctx, cls, event), "start": start, "end": end,
                       "attendees": [event["sender_address"]], "description": "Scheduled by Job Sourcer.",
                       "add_video_link": sch.add_video_link, "location": ""}
        return {**empty, "kind": "accept_proposed", "accepted_label": label, "booking": booking,
                "checks": [{"start": start, "end": end, "label": label, "free": True, "ok": True, "problems": []}]}

    # -- re-prioritization ----------------------------------------------------------
    async def reprioritize(self, hub: MCPHub) -> dict[str, int]:
        """React to what changed since a draft was written: stage, calendar, the clock."""
        out = {"escalated": 0, "redrafted": 0}
        min_notice = self.cfg.scheduling.min_notice_hours
        now = utcnow()
        for d in self.db.query("SELECT * FROM drafts WHERE status IN ('pending', 'approved', 'edited') ORDER BY id"):
            opp = self.db.get_opportunity(d["opportunity_id"])
            reasons = loads(d["route_reasons_json"], []) or []
            if opp and opp["stage"] == "offer" and not any(r.startswith("Offer room") for r in reasons):
                reasons.insert(0, "Offer room: autonomy is revoked for this thread; you handle it personally.")
                self.db.update("drafts", d["id"], {"priority": "high", "route": "ask",
                                                   "route_reasons_json": dumps(reasons)})
                out["escalated"] += 1
            actions = loads(d["final_actions_json"], None) or loads(d["actions_json"], []) or []
            booking = next((a["payload"] for a in actions if a["type"] == "create_event" and not a.get("skip")), None)
            stale = ""
            if booking:
                start = parse_iso(booking.get("start"))
                if start and start < now + timedelta(hours=min_notice):
                    stale = "The proposed meeting time is too close or has passed."
                elif hub.enabled("calendar"):
                    check = await hub.call("calendar", "check_availability", {
                        "start": booking["start"], "end": booking["end"],
                        "buffer_minutes": self.cfg.scheduling.buffer_minutes})
                    if not check.get("free"):
                        stale = "Something was booked over the proposed meeting time."
            slots = (loads(d["analysis_json"], {}) or {}).get("schedule", {}).get("slots", []) or []
            if not stale and slots and any((parse_iso(s.get("start")) or now) <= now for s in slots):
                stale = "Some of the times this reply offers have passed."
            if stale:
                self.db.update("drafts", d["id"], {"status": "superseded",
                                                   "status_reason": f"{stale} Re-drafted with fresh availability."})
                self.db.update("events", d["event_id"], {"status": "new", "attempts": 0})
                out["redrafted"] += 1
        return out

    def rescore(self) -> None:
        for d in self.db.query("SELECT * FROM drafts WHERE status IN ('pending', 'approved', 'edited', 'rejected')"):
            score, why = priority.compute(self.cfg, self.db, d)
            self.db.update("drafts", d["id"], {"priority_score": score, "priority_why_json": dumps(why)})

    def _due(self, source: str, minutes: int) -> bool:
        last = parse_iso(self.db.kv_get(f"last_poll:{source}"))
        return last is None or (utcnow() - last).total_seconds() >= minutes * 60

    # -- Phase 1: ingestion -------------------------------------------------------
    def _insert_event(self, values: dict[str, Any]) -> int | None:
        exists = self.db.query_one("SELECT id FROM events WHERE source = ? AND external_id = ?",
                                   (values["source"], values["external_id"]))
        if exists:
            return None
        values.setdefault("created_at", now_iso())
        values.setdefault("status", "new")
        return self.db.insert("events", values)

    def _email_thread_key(self, msg: dict[str, Any]) -> str:
        refs = list(msg.get("references") or [])
        candidates = ([msg["in_reply_to"]] if msg.get("in_reply_to") else []) + refs[::-1]
        for mid in candidates:
            row = self.db.query_one("SELECT thread_key FROM message_ids WHERE message_id = ?", (mid,))
            if row:
                return row["thread_key"]
        return refs[0] if refs else (msg.get("in_reply_to") or msg["message_id"])

    async def ingest_email(self, hub: MCPHub) -> int:
        since = int(self.db.kv_get("mail:last_uid", 0) or 0)
        res = await hub.call("mail", "list_messages", {"since_uid": since})
        me = (self.cfg.profile.email or self.cfg.mail.username or "").lower()
        count = 0
        for msg in res.get("messages", []):
            sender = msg.get("from_email", "").lower()
            if sender == me:
                continue
            thread_key = self._email_thread_key(msg)
            self.db.execute("INSERT OR IGNORE INTO message_ids (message_id, thread_key) VALUES (?, ?)",
                            (msg["message_id"], thread_key))
            subject, body = msg.get("subject", ""), msg.get("body_text", "")
            known_thread = self.db.query_one("SELECT 1 FROM opportunity_threads WHERE thread_key = ?", (thread_key,))
            known_sender = self.db.query_one("SELECT 1 FROM opportunities WHERE lower(recruiter_email) = ?", (sender,))
            why = None
            if channels.is_linkedin_address(sender):
                # Never answered by email. A message notification means: read and answer it on LinkedIn, now.
                notice = channels.is_linkedin_message_notice(sender, subject, body)
                if notice and self.cfg.linkedin.backend != "disabled":
                    self.db.kv_set("linkedin:check_now", True)
                    why = "LinkedIn message notification: ignored here; the message is read and answered on LinkedIn."
                elif notice:
                    why = ("LinkedIn message notification: ignored here. Connect LinkedIn (Setup step 6) so Job "
                           "Sourcer can answer it on LinkedIn.")
                else:
                    why = "Email from LinkedIn (alert or digest): never answered by email."
            elif not (self.cfg.mail.prefilter == "all" or known_thread or known_sender
                      or PREFILTER.search(f"{subject}\n{body}") or msg.get("has_calendar_invite")):
                why = "Filtered out: no job-search signal (no LLM call made)."
            event_id = self._insert_event({
                "source": "email", "external_id": msg["message_id"], "thread_key": thread_key,
                "sender_name": msg.get("from_name", ""), "sender_address": sender,
                "subject": subject, "body_text": body,
                "received_at": msg.get("date") or now_iso(), "raw_json": dumps(msg),
                "status": "ignored" if why else "new", "error": why,
            })
            if event_id:
                count += 1
                who = msg.get("from_name") or sender
                activity.record(self.db, "message", f"Email from {who}: {subject or '(no subject)'}"
                                + (f". {why}" if why else ""), event_id=event_id)
        self.db.kv_set("mail:last_uid", res.get("last_uid", since))
        return count

    async def ingest_linkedin(self, hub: MCPHub) -> int:
        since = self.db.kv_get("linkedin:since", "")
        res = await hub.call("linkedin", "list_conversations", {"since": since, "limit": 30})
        count = 0
        newest = since
        for conv in res.get("conversations", []):
            msgs = conv.get("messages", [])
            last_out = max((i for i, m in enumerate(msgs) if m["from"] == "me"), default=-1)
            new_in = [m for m in msgs[last_out + 1:] if m["from"] == "them"]
            if not new_in:
                continue
            last = new_in[-1]
            body = "\n\n".join(m["text"] for m in new_in)
            event_id = self._insert_event({
                "source": "linkedin", "external_id": f"{conv['id']}:{last['id']}", "thread_key": conv["id"],
                "sender_name": conv.get("participant_name", ""),
                "sender_address": conv.get("profile_url") or f"linkedin:{conv['id']}",
                "subject": conv.get("participant_headline", ""), "body_text": body,
                "received_at": last.get("sent_at") or now_iso(),
                "raw_json": dumps({"conversation_id": conv["id"], "profile_url": conv.get("profile_url", ""),
                                   "headline": conv.get("participant_headline", ""),
                                   "history": msgs[: max(last_out + 1, 0)]}),
            })
            if event_id:
                count += 1
                activity.record(self.db, "message", f"LinkedIn message from {conv.get('participant_name') or 'someone'}"
                                f" ({len(new_in)} new)", event_id=event_id)
            if not newest or (last.get("sent_at") or "") > newest:
                newest = last.get("sent_at") or newest
        if newest:
            self.db.kv_set("linkedin:since", newest)
        return count

    async def ingest_calendar(self, hub: MCPHub) -> int:
        res = await hub.call("calendar", "pending_invites", {"days": self.cfg.calendar.invite_lookahead_days})
        count = 0
        for inv in res.get("invites", []):
            organizer = inv.get("organizer", {})
            when = f"When: {fmt_range(inv['start'], inv['end'], self.cfg.profile.timezone)}"
            event_id = self._insert_event({
                "source": "calendar", "external_id": f"{inv['id']}:{inv['start']}", "thread_key": inv["id"],
                "sender_name": organizer.get("name", ""), "sender_address": organizer.get("email", "").lower(),
                "subject": inv.get("title", ""), "body_text": f"{inv.get('description', '')}\n{when}".strip(),
                "received_at": now_iso(), "raw_json": dumps(inv),
            })
            if event_id:
                count += 1
                activity.record(self.db, "message", f"Calendar invite from {organizer.get('name') or organizer.get('email') or 'someone'}"
                                f": {inv.get('title', '')} ({when[6:]}). It is on your calendar; whether to answer "
                                "Yes/No is up to you.", event_id=event_id)
        return count + await self.detect_meeting_changes(hub)

    async def detect_meeting_changes(self, hub: MCPHub) -> int:
        """A job meeting was moved, cancelled, or the recruiter declined it: that is news too."""
        now = utcnow()
        res = await hub.call("calendar", "list_events", {
            "start": to_iso(now - timedelta(hours=1)),
            "end": to_iso(now + timedelta(days=self.cfg.calendar.invite_lookahead_days))})
        everything = {e["id"]: e for e in res.get("events", [])}
        meetings = {e["id"]: e for e in notify.job_meetings(self.db, list(everything.values()))}
        me = (self.cfg.profile.email or "").lower()

        def snap(e: dict[str, Any]) -> dict[str, Any]:
            others = [a for a in e.get("attendees", []) if a.get("email", "").lower() != me]
            return {"title": e.get("title", ""), "start": e["start"], "end": e["end"],
                    "organizer": e.get("organizer", {}), "others": others,
                    "declined": sorted(a.get("email", "") for a in others if a.get("response") == "declined")}

        before: dict[str, Any] = self.db.kv_get("meeting_snapshot", {}) or {}
        current = {mid: snap(e) for mid, e in meetings.items()}
        count = 0
        for mid, old in before.items():
            new = current.get(mid)
            change = ""
            if new is None:
                still = everything.get(mid)
                if still is None and (parse_iso(old["start"]) or now) > now:
                    change = "cancelled"
            elif parse_iso(new["start"]) != parse_iso(old["start"]) or parse_iso(new["end"]) != parse_iso(old["end"]):
                change = "rescheduled"
            elif set(new["declined"]) - set(old["declined"]):
                change = "declined"
            if not change:
                continue
            ref = new or old
            contact = ref["organizer"] if (ref["organizer"].get("email", "").lower() not in ("", me)) else \
                (ref["others"][0] if ref["others"] else {})
            when_old = fmt_range(old["start"], old["end"], self.cfg.profile.timezone)
            if change == "cancelled":
                text = f"This meeting was cancelled (it was {when_old})."
            elif change == "declined":
                who = ", ".join(sorted(set(new["declined"]) - set(old["declined"])))
                text = f"{who} declined this meeting ({when_old})."
            else:
                text = f"Moved from {when_old} to {fmt_range(ref['start'], ref['end'], self.cfg.profile.timezone)}."
            event_id = self._insert_event({
                "source": "calendar", "external_id": f"{mid}:{change}:{ref['start']}", "thread_key": mid,
                "sender_name": contact.get("name", ""), "sender_address": contact.get("email", "").lower(),
                "subject": f"{change.capitalize()}: {ref['title']}", "body_text": text, "received_at": now_iso(),
                "raw_json": dumps({"kind": "meeting_change", "change": change, "id": mid, "old": old, "new": new,
                                   "start": ref["start"], "end": ref["end"]}),
            })
            count += 1 if event_id else 0
            if event_id:
                activity.record(self.db, "message", f"Meeting {change}: {ref['title']}. {text}", event_id=event_id)
        self.db.kv_set("meeting_snapshot", current)
        return count

    def detect_follow_ups(self) -> int:
        """Keep pushing: draft a follow-up when a recruiter has gone quiet after your last message."""
        days = self.cfg.autonomy.follow_up_after_days
        count = 0
        for opp in self.db.query("SELECT * FROM opportunities"):
            if opp["stage"] not in ACTIVE_STAGES or opp["stage"] == "offer" or not opp["last_outbound_at"]:
                continue
            if opp["last_inbound_at"] and opp["last_inbound_at"] > opp["last_outbound_at"]:
                continue
            if days_between(opp["last_outbound_at"]) < days:
                continue
            last = self.db.query_one(
                "SELECT o.* FROM outbound o JOIN opportunity_threads t ON t.thread_key = o.thread_key "
                "WHERE t.opportunity_id = ? ORDER BY o.id DESC LIMIT 1", (opp["id"],))
            if not last:
                continue
            event_id = self._insert_event({
                "source": "steward", "external_id": f"followup:{opp['id']}:{opp['last_outbound_at']}",
                "thread_key": last["thread_key"], "sender_name": opp.get("recruiter_name") or "",
                "sender_address": opp.get("recruiter_email") or "",
                "subject": last.get("subject") or "", "opportunity_id": opp["id"],
                "body_text": f"(No reply from {opp.get('recruiter_name') or 'the recruiter'} for "
                             f"{int(days_between(opp['last_outbound_at']))} days since your last message.)",
                "received_at": now_iso(),
                "raw_json": dumps({"follow_up": True, "channel": last["channel"], "last_outbound": last}),
            })
            if event_id:
                count += 1
        return count

    # -- Phase 2 + 3: process -----------------------------------------------------
    def llm_paused(self) -> dict[str, Any] | None:
        """Claude is unavailable (e.g. your plan's usage limit) until this time; messages wait in the queue."""
        pause = self.db.kv_get("llm_paused")
        if pause and (parse_iso(pause.get("until")) or utcnow()) > utcnow():
            return pause
        return None

    def _pause_llm(self, exc: LLMUnavailable, errors: list[str]) -> None:
        until = exc.retry_at or (utcnow() + timedelta(minutes=15))
        self.db.kv_set("llm_paused", {"until": to_iso(until), "reason": str(exc)[:300]})
        errors.append(f"Claude unavailable until {fmt_local(until, self.cfg.profile.timezone)}: {exc}")

    async def process_pending(self, hub: MCPHub, errors: list[str]) -> tuple[int, int]:
        processed = auto = 0
        waiting = self.db.query("SELECT * FROM events WHERE status = 'new' ORDER BY received_at, id")
        if not waiting or self.llm_paused():
            return processed, auto
        try:
            ctx = self.ctx(hub)
        except LLMUnavailable as exc:
            self._pause_llm(exc, errors)
            return processed, auto
        self.db.kv_set("llm_paused", None)
        for row in waiting:
            event = hydrate(row)
            try:
                result = await self.process_event(ctx, event)
                processed += 1
                auto += 1 if result == "auto" else 0
            except LLMUnavailable as exc:
                # Not the message's fault: keep it (and everything after it) queued, retry after the reset.
                self._pause_llm(exc, errors)
                break
            except (LLMRefusal, LLMError, MCPToolError) as exc:
                attempts = event["attempts"] + 1
                give_up = isinstance(exc, LLMRefusal) or attempts >= self.cfg.autonomy.max_processing_attempts
                self.db.update("events", event["id"], {"attempts": attempts, "error": str(exc)[:1000],
                                                       "status": "error" if give_up else "new"})
                if give_up:
                    self._escalate_unprocessed(event, str(exc))
                errors.append(f"event {event['id']}: {exc}")
        return processed, auto

    def _escalate_unprocessed(self, event: dict[str, Any], reason: str) -> None:
        """If Job Sourcer cannot reason about a message, it hands it to you rather than guessing."""
        self.db.insert("drafts", {
            "event_id": event["id"], "channel": event["source"], "summary": event.get("subject") or "Message",
            "actions_json": "[]", "analysis_json": dumps({"classification": {"categories": ["other"]}}),
            "confidence": 0.0, "job_advancement": 0.0, "routing_band": "LOW", "risk_level": "high",
            "priority": "high", "route": "ask",
            "route_reasons_json": dumps([f"Job Sourcer could not process this message: {reason}"]),
            "status": "pending", "created_at": now_iso(),
        })

    def thread_history(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if event["source"] == "linkedin":
            return [{"direction": "out" if m["from"] == "me" else "in", "sender": event.get("sender_name"),
                     "at": m.get("sent_at", ""), "text": m.get("text", "")}
                    for m in event["raw"].get("history", [])]
        if not event.get("thread_key"):
            return []
        items = []
        for e in self.db.query("SELECT * FROM events WHERE thread_key = ? AND id != ? AND source = ? "
                               "ORDER BY received_at", (event["thread_key"], event["id"], event["source"])):
            if e["source"] == "steward":
                continue
            items.append({"direction": "in", "sender": e["sender_name"], "at": e["received_at"],
                          "text": e["body_text"] or ""})
        for o in self.db.query("SELECT * FROM outbound WHERE thread_key = ? ORDER BY sent_at", (event["thread_key"],)):
            items.append({"direction": "out", "at": o["sent_at"], "text": o["body"] or ""})
        return sorted(items, key=lambda m: m.get("at") or "")

    @staticmethod
    def _fixed_classification(category: str, summary: str, event: dict[str, Any], needs_reply: bool) -> dict[str, Any]:
        """Events Job Sourcer generated itself need no LLM triage."""
        return {"is_recruiter_related": True, "needs_reply": needs_reply, "summary": summary,
                "company": "", "role": "", "recruiter_name": event.get("sender_name", ""), "phone_number": "",
                "location": "", "categories": [category], "primary_category": category, "questions": [],
                "technologies": [], "proposed_times": [], "meeting_link": "", "stage_signal": "none",
                "urgency": "normal", "suspicious": False, "risk_level": risk_of([category])}

    def _reply_target(self, opp: dict[str, Any] | None) -> dict[str, Any] | None:
        """Where to answer someone who reached you by phone: their latest email or LinkedIn thread."""
        if not opp:
            return None
        ev = next((e for e in self.db.query(
            "SELECT * FROM events WHERE opportunity_id = ? AND source IN ('email', 'linkedin') ORDER BY received_at DESC",
            (opp["id"],)) if not (e["source"] == "email" and channels.is_linkedin_address(e["sender_address"]))), None)
        if ev and ev["source"] == "linkedin":
            return {"channel": "linkedin", "conversation_id": ev["thread_key"], "recipient": ev["sender_name"],
                    "thread_key": ev["thread_key"]}
        if ev:
            raw = loads(ev["raw_json"], {}) or {}
            subject = re.sub(r"^(re:\s*)+", "", ev["subject"] or "", flags=re.I)
            refs = list(raw.get("references") or [])
            if raw.get("message_id"):
                refs.append(raw["message_id"])
            return {"channel": "email", "to": [ev["sender_address"]], "subject": f"Re: {subject}" if subject else "",
                    "in_reply_to": raw.get("message_id", ""), "references": refs, "thread_key": ev["thread_key"]}
        if opp.get("recruiter_email") and not channels.is_linkedin_address(opp["recruiter_email"]):
            what = " - ".join(x for x in (opp.get("role"), opp.get("company")) if x)
            return {"channel": "email", "to": [opp["recruiter_email"]], "subject": f"Sorry I missed your call{': ' + what if what else ''}",
                    "in_reply_to": "", "references": []}
        return None

    async def process_event(self, ctx: AgentContext, event: dict[str, Any]) -> str:
        thread = self.thread_history(event)
        raw = event["raw"]
        follow_up = bool(raw.get("follow_up"))
        meeting_change = raw.get("kind") == "meeting_change"
        if follow_up:
            cls = self._fixed_classification("follow_up", "Follow-up on a quiet thread", event, True)
        elif meeting_change:
            cls = self._fixed_classification("meeting_change", f"{event.get('subject')}. {event.get('body_text')}",
                                             event, raw.get("change") in ("cancelled", "declined"))
        else:
            cls = await classifier.classify(ctx, event, thread)
        # An online assessment is never missed, whatever the classifier made of the email.
        test = assessments.detect({**raw, "subject": event.get("subject", ""), "body_text": event.get("body_text", ""),
                                   "received_at": event.get("received_at")},
                                  self.cfg.profile.timezone, cls.get("company", "")) \
            if event["source"] == "email" else None
        if test:
            cls["is_recruiter_related"] = True
            if assessments.from_platform(event.get("sender_address", "")):
                cls["needs_reply"] = False  # the platform itself, not a person
        self.db.update("events", event["id"], {"classification_json": dumps(cls)})

        if not cls["is_recruiter_related"]:
            self.db.update("events", event["id"], {"status": "ignored", "processed_at": now_iso(),
                                                   "error": "Not related to your job search."})
            return "ignored"

        call = "call_notification" in cls["categories"]
        if follow_up:
            opp = self.db.get_opportunity(event.get("opportunity_id"))
        elif meeting_change:
            opp = pipeline.find_for_event(self.db, event, None)
        elif call:
            opp = pipeline.find_for_call(self.db, cls)
        else:
            opp = pipeline.upsert_for_event(self.db, event, cls)
        if opp and (follow_up or meeting_change):
            cls["company"], cls["role"] = opp.get("company") or "", opp.get("role") or ""
        if call:
            via = self._reply_target(opp)
            if via:
                raw["reply_via"] = via
                event["thread_key"] = via.get("thread_key") or event.get("thread_key")
                self.db.update("events", event["id"], {"raw_json": dumps(raw), "thread_key": event["thread_key"]})
            else:
                self._call_back_item(event, cls, opp)
                return "queued"
        if test:
            self.record_assessment(event, test, opp)
        tasks_mod.suggest_from_classification(self.cfg, self.db, self.ledger, cls, event["id"],
                                              opp["id"] if opp else None)
        if opp and ctx.hub.enabled("notion"):
            try:
                opp = await pipeline.notion_pull(ctx.hub, self.db, opp)
            except MCPToolError as exc:
                log.warning("notion pull failed: %s", exc)
        self.db.update("events", event["id"], {"opportunity_id": opp["id"] if opp else None})

        # A newer message in the same thread makes any draft that has not gone out stale (undecided, waiting,
        # deferred, or failed): re-executing it later would answer an old message.
        self.db.execute(
            "UPDATE drafts SET status = 'superseded', status_reason = 'A newer message arrived in this thread.', "
            "closed_at = ? WHERE status IN ('pending', 'approved', 'edited', 'rejected', 'queued', 'deferred', 'failed') "
            "AND event_id IN (SELECT id FROM events WHERE thread_key = ? AND source = ? AND id != ?)",
            (now_iso(), event.get("thread_key") or "-", event["source"], event["id"]))

        added = await self._add_invites(ctx, event, cls, opp)
        evidence = await cv_analyst.analyze(ctx, cls)
        # Once the invitation is on the calendar, nothing else gets booked for this message.
        sched = await scheduler.plan(ctx, event, {**cls, "proposed_times": []} if added else cls)
        if added and sched["kind"] == "none":
            sched["kind"], sched["accepted_label"] = "invite_added", "; ".join(added)
        if event["source"] == "calendar":
            # Ask the organizer to move a conflicting invite; offer new times when a meeting was dropped.
            needs_text = sched["kind"] == "invite_conflict" or (meeting_change and cls["needs_reply"])
        else:
            needs_text = cls["needs_reply"]
        if not needs_text and not sched.get("booking") and not sched.get("rsvp"):
            self.db.update("events", event["id"], {"status": "info", "processed_at": now_iso()})
            return "info"

        prefs = decisions.active_preferences(self.db, cls["categories"], (opp or {}).get("company") or cls.get("company", ""),
                                             event.get("sender_address", ""))
        examples = fewshot.retrieve(self.ledger, event, cls)
        if needs_text:
            drafted = await drafter.draft(ctx, event, thread, cls, evidence, sched, opp, prefs, examples)
            review = await drafter.critique(ctx, event, cls, drafted, evidence, sched, opp)
        else:
            drafted = {"reply_subject": "", "reply_body": "", "answered_questions": [], "unanswered_questions": [],
                       "commitments": [], "next_step": "", "confidence": 0.95,
                       "rationale": "Calendar-only action; nothing to write."}
            review = {"job_advancement_score": 0.85, "confidence": 0.95, "verdict": "send", "issues": [], "risks": []}

        actions = self.build_actions(event, cls, drafted, sched, opp)
        decision = route(self.cfg, self.db, event=event, cls=cls, evidence=evidence, sched=sched, drafted=drafted,
                         review=review, opp=opp)
        draft_id = self._save_draft(event, cls, evidence, sched, drafted, review, actions, decision, opp, examples,
                                    prefs)
        tasks_mod.link_draft(self.db, event["id"], draft_id)
        for rule_id in decision.matched_rule_ids + decision.shadow_rule_ids:
            self.db.execute("UPDATE rules SET times_matched = times_matched + 1, last_matched_at = ? WHERE id = ?",
                            (now_iso(), rule_id))
        self.db.update("events", event["id"], {"status": "processed", "processed_at": now_iso()})

        if decision.route in ("auto_approve", "auto_send") and actions:
            draft = self.db.get_draft(draft_id)
            self.ledger.append("auto_execution", {
                **decisions.ledger_payload(self.db, draft, "auto", actions),
                "rule_id": decision.matched_rule_id, "tier": decision.route,
            }, event_id=event["id"], draft_id=draft_id, rule_id=decision.matched_rule_id)
            self.db.update("drafts", draft_id, {"status": "queued", "decided_at": now_iso()})
            await Executor(self.cfg, self.db, self.ledger, ctx.hub).execute_draft(draft_id)
            return "auto"
        return "queued"

    def record_assessment(self, event: dict[str, Any], test: dict[str, Any], opp: dict[str, Any] | None) -> int:
        """An online assessment becomes an open task (link + deadline), so it shows under Upcoming deadlines.

        A reminder for the same test (same title and deadline, or same link) updates that task instead."""
        tz_name = self.cfg.profile.timezone
        title = assessments.task_title({**test, "company": test["company"] or (opp or {}).get("company") or ""})
        due_label = fmt_local(test["due_at"], tz_name) if test["due_at"] else ""
        details = " · ".join(x for x in (
            f"{test['platform']} assessment" if test["platform"] else "Online assessment",
            test["duration"] and f"duration {test['duration']}", due_label and f"deadline {due_label}") if x)
        same = self.db.query_one(
            "SELECT * FROM tasks WHERE kind = 'assessment' AND status != 'dismissed' AND ("
            "(link IS NOT NULL AND link = ?) OR (instr(lower(title), lower(?)) > 0 AND ifnull(due_at, '') = ?))",
            (test["link"] or "-", test["title"], test["due_at"] or ""))
        if same:
            fields = {k: v for k, v in (("link", test["link"]), ("due_at", test["due_at"])) if v and not same[k]}
            if fields:
                tasks_mod.act(self.cfg, self.db, self.ledger, same["id"], "update", fields)
            activity.record(self.db, "message", f"Reminder for an assessment already in your tasks: {same['title']}"
                            + (f" (due {due_label})" if due_label else ""), event_id=event["id"])
            return same["id"]
        task_id = tasks_mod.add(self.cfg, self.db, self.ledger, title, details=details, kind="assessment",
                                link=test["link"], due_at=test["due_at"], opportunity_id=opp["id"] if opp else None,
                                origin="steward", status="open", event_id=event["id"])
        activity.record(self.db, "message", f"Assessment found: {title}. "
                        + (f"Deadline {due_label}: added to your tasks and to Upcoming meetings & deadlines."
                           if due_label else "No deadline in the email: added to your tasks; set the due date there.")
                        + ("" if test["link"] else " The test link is in the email."),
                        "info" if due_label else "warn", event_id=event["id"])
        return task_id

    async def _add_invites(self, ctx: AgentContext, event: dict[str, Any], cls: dict[str, Any],
                           opp: dict[str, Any] | None) -> list[str]:
        """A recruiter's meeting invitation in an email goes on your calendar right away, without asking.

        Added as is (no reply to the recruiter, nobody invited) and never twice. Returns the times added."""
        raw = event["raw"]
        found = [i for i in raw.get("invites") or [] if i.get("method") != "cancel"]
        if event["source"] != "email" or not (found or raw.get("add_to_calendar")):
            return []
        who = event.get("sender_name") or event.get("sender_address")
        if not found:
            activity.record(self.db, "calendar", f"Email from {who} offers 'Add to Calendar', but its date and time "
                            "could not be read (no invitation file, and the link carries no time). Add it yourself; "
                            "the message is in your briefing.", "warn", event_id=event["id"])
            return []
        if cls.get("suspicious"):
            activity.record(self.db, "calendar", f"Invitation from {who} not added: the message looks suspicious.",
                            "warn", event_id=event["id"])
            return []
        if not ctx.hub.enabled("calendar"):
            activity.record(self.db, "calendar", f"Invitation from {who} not added: the calendar is not connected "
                            "(Setup step 5).", "warn", event_id=event["id"])
            return []
        tz_name = self.cfg.profile.timezone
        labels = [f"{i['title']}, {fmt_range(i['start'], i['end'], tz_name)}" for i in found]
        actions = [{"type": "add_to_calendar", "payload": {
            "uid": i["uid"], "title": i["title"], "start": i["start"], "end": i["end"], "location": i["location"],
            "description": i["description"], "organizer_email": i["organizer_email"] or event.get("sender_address", ""),
            "organizer_name": i["organizer_name"] or event.get("sender_name", "")}} for i in found]
        draft_id = self.db.insert("drafts", {
            "event_id": event["id"], "opportunity_id": opp["id"] if opp else None, "channel": "calendar",
            "summary": f"Added to your calendar: {'; '.join(labels)}", "actions_json": dumps(actions),
            "analysis_json": dumps({"classification": cls}), "confidence": 1.0, "job_advancement": 1.0,
            "routing_band": "HIGH", "risk_level": "low", "priority": "normal", "route": "auto_approve",
            "route_reasons_json": dumps(["Recruiters' meeting invitations go on your calendar automatically, "
                                         "without a reply to them."]),
            "status": "queued", "created_at": now_iso(), "decided_at": now_iso(),
        })
        self.ledger.append("auto_execution", {
            **decisions.ledger_payload(self.db, self.db.get_draft(draft_id), "auto", actions),
            "rule_id": None, "tier": "standing_instruction",
            "instruction": "Add recruiters' meeting invitations to my calendar without asking (no reply).",
        }, event_id=event["id"], draft_id=draft_id)
        await Executor(self.cfg, self.db, self.ledger, ctx.hub).execute_draft(draft_id)
        return [fmt_range(i["start"], i["end"], tz_name) for i in found]

    def build_actions(self, event: dict[str, Any], cls: dict[str, Any], drafted: dict[str, Any],
                      sched: dict[str, Any], opp: dict[str, Any] | None) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        if sched.get("booking"):
            actions.append({"type": "create_event", "payload": sched["booking"]})
        if sched.get("rsvp"):
            actions.append({"type": "rsvp_event", "payload": sched["rsvp"]})
        body = drafted.get("reply_body", "")
        if not body:
            return actions
        raw = event["raw"]
        via = raw.get("reply_via")
        if via and via["channel"] == "linkedin":
            actions.append({"type": "send_linkedin", "payload": {"conversation_id": via["conversation_id"],
                                                                 "text": body, "recipient": via.get("recipient", "")}})
            return actions
        if via:
            actions.append({"type": "send_email", "payload": {
                "to": via["to"], "cc": [], "subject": via.get("subject") or drafted.get("reply_subject") or "",
                "body": body, "in_reply_to": via.get("in_reply_to", ""), "references": via.get("references", [])}})
            return actions
        channel = event["source"]
        if channel == "steward":
            channel = raw.get("channel", "email")
        if channel == "linkedin":
            conv = raw.get("conversation_id") or event.get("thread_key")
            if raw.get("last_outbound"):
                conv = raw["last_outbound"].get("thread_key") or conv
            actions.append({"type": "send_linkedin", "payload": {"conversation_id": conv, "text": body,
                                                                 "recipient": event.get("sender_name", "")}})
            return actions
        to = event.get("sender_address") or (opp or {}).get("recruiter_email") or ""
        if channels.is_linkedin_address(to) or channels.is_no_reply(to) or assessments.from_platform(to):
            return actions  # never email LinkedIn, a no-reply address, or an assessment platform
        subject = drafted.get("reply_subject") or event.get("subject") or ""
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        in_reply_to, references = "", []
        if event["source"] == "email":
            in_reply_to = raw.get("message_id", "")
            references = list(raw.get("references") or [])
        elif raw.get("last_outbound"):
            in_reply_to = raw["last_outbound"].get("external_id", "")
            references = [in_reply_to] if in_reply_to else []
        if to:
            actions.append({"type": "send_email", "payload": {
                "to": [to], "cc": [], "subject": subject, "body": body, "in_reply_to": in_reply_to,
                "references": references}})
        return actions

    def _call_back_item(self, event: dict[str, Any], cls: dict[str, Any], opp: dict[str, Any] | None) -> None:
        """A recruiter called and Job Sourcer has no thread to answer in: put it at the top for you."""
        number = cls.get("phone_number") or "the number in the notification"
        who = cls.get("recruiter_name") or "A recruiter"
        self.db.insert("drafts", {
            "event_id": event["id"], "opportunity_id": opp["id"] if opp else None, "channel": event["source"],
            "summary": f"{who}{' (' + cls['company'] + ')' if cls.get('company') else ''} called you",
            "actions_json": "[]", "analysis_json": dumps({"classification": cls}),
            "confidence": 0.0, "job_advancement": 0.0, "routing_band": "LOW", "risk_level": cls.get("risk_level", "medium"),
            "priority": "high", "route": "ask", "status": "pending", "created_at": now_iso(),
            "route_reasons_json": dumps([f"They called you. Call back {number}: no email or LinkedIn thread is known "
                                         "for them yet, so Job Sourcer cannot reply in writing."]),
        })
        self.db.update("events", event["id"], {"status": "processed", "processed_at": now_iso()})

    def _save_draft(self, event, cls, evidence, sched, drafted, review, actions, decision: RouteDecision, opp,
                    examples, prefs) -> int:
        summary = cls.get("summary") or event.get("subject") or ""
        who = cls.get("company") or (opp or {}).get("company") or event.get("sender_name") or ""
        return self.db.insert("drafts", {
            "event_id": event["id"],
            "opportunity_id": opp["id"] if opp else None,
            "channel": event["source"] if event["source"] != "steward" else event["raw"].get("channel", "email"),
            "summary": f"{who}: {summary}" if who and who not in summary else summary,
            "actions_json": dumps(actions),
            "analysis_json": dumps({
                "classification": cls, "evidence": evidence, "schedule": sched, "draft": drafted, "review": review,
                "examples_used": len(examples), "preferences_used": prefs,
            }),
            "confidence": decision.confidence,
            "job_advancement": decision.job_advancement,
            "routing_band": decision.band,
            "risk_level": cls.get("risk_level", "medium"),
            "priority": decision.priority,
            "route": decision.route,
            "route_reasons_json": dumps(decision.blockers + decision.reasons),
            "matched_rule_id": decision.matched_rule_id,
            "shadow_rule_ids_json": dumps(decision.shadow_rule_ids),
            "status": "pending",
            "created_at": now_iso(),
        })

    async def sync_notion(self, hub: MCPHub, errors: list[str]) -> None:
        if not hub.enabled("notion"):
            return
        for opp in self.db.query("SELECT id FROM opportunities WHERE notion_synced_at IS NULL OR updated_at > notion_synced_at"):
            try:
                await pipeline.notion_push(hub, self.db, opp["id"])
            except MCPToolError as exc:
                errors.append(f"notion: {exc}")
                return

    async def run_forever(self, tick_seconds: int = 60) -> None:
        while True:
            try:
                summary = await self.cycle()
                if summary["processed"] or summary["errors"]:
                    log.info("cycle: %s", {k: summary[k] for k in ("ingested", "processed", "auto_executed", "errors")})
            except Exception as exc:  # noqa: BLE001 - the daemon must survive anything
                log.exception("cycle failed: %s", exc)
            await asyncio.sleep(tick_seconds)
