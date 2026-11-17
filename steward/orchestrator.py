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
import os
import re
from datetime import timedelta
from typing import Any

from steward import (acknowledgments, activity, assessments, channels, decisions, linkedin_bridge, mail_cleanup,
                     notify, pipeline, priority, reconcile, sound)
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
from steward.mcp_hub import ExecutorCapability, IntegrationDisabled, MCPHub, MCPToolError
from steward.taxonomy import ACTIVE_STAGES, CLOSED_STAGES, STAGES, risk_of
from steward.timeutil import days_between, fmt_local, fmt_range, now_iso, parse_iso, to_iso, tz, utcnow
from steward.work_queue import queue_actions

log = logging.getLogger("steward")

PREFILTER = re.compile(
    r"\b(recruit|hiring|hire|role|position|opportunit|job|interview|candidate|talent|offer|salary|compensation|"
    r"resume|cv|your (background|profile|experience)|engineer|developer|team|headcount|availability|schedule|"
    r"voicemail|missed call|assessment|coding (test|challenge)|online test|hackerrank|codility|codesignal|"
    r"testgorilla|hackerearth|coderbyte|karat|mettl|imocha|testdome|devskiller|coderpad|codingame|hirevue)",
    re.IGNORECASE,
)
# WhatsApp chats are often in Spanish: a chat is looked at when it has either signal.
REVIEW_INSTRUCTION = ("Write a short, warm follow-up to the recruiter about this position: ask them for an update on "
                      "where things stand and whether the position is still open. Do not offer meeting times.")
WAITING_ON_YOU = ("pending", "approved", "edited", "rejected", "queued", "deferred", "failed")
JOB_WORDS_ES = re.compile(
    r"\b(vacante|puesto|reclut|entrevista|oferta (laboral|de (trabajo|empleo))|empleo|trabajo|postula|candidat|"
    r"sueldo|salario|curr[ií]cul|oportunidad|contrataci[oó]n|recursos humanos|headhunter|talento)",
    re.IGNORECASE,
)


SPANISH = re.compile(r"\b(hola|gracias|saludos|entrevista|reuni[oó]n|invitaci[oó]n|para|est[aá]s?|tu|su|del|que)\b",
                     re.IGNORECASE)
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def _attendance_note(event: dict[str, Any], found: list[dict[str, Any]], name: str, tz_name: str) -> str:
    """The short "I will attend" reply to an invitation, in the language it was written in."""
    last, _, given = (event.get("sender_name") or "").partition(",")  # "Lee, Dana" or "Dana Lee"
    first = ((given or last).split() or [""])[0]
    me = (name or "").split()[0] if name and name != "Your Name" else ""
    if len(SPANISH.findall(f"{event.get('subject', '')}\n{event.get('body_text', '')}")) >= 3:
        def es(i: dict[str, Any]) -> str:
            s = parse_iso(i["start"]).astimezone(tz(tz_name))
            return f"el {DIAS[s.weekday()]} {s.day} de {MESES[s.month - 1]} a las {s.strftime('%H:%M')} ({s.strftime('%Z')})"
        return (f"Hola{' ' + first if first else ''},\n\nGracias por la invitación. Confirmo mi asistencia "
                f"{' y '.join(es(i) for i in found)}.\n\nSaludos,\n{me}").rstrip()
    when = " and ".join(fmt_range(i["start"], i["end"], tz_name) for i in found)
    return (f"Hi{' ' + first if first else ''},\n\nThank you for the invitation. I confirm I will attend ({when}).\n\n"
            f"Best regards,\n{me}").rstrip()


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
                ("whatsapp", self.ingest_whatsapp, self.cfg.whatsapp.poll_minutes),
                ("calendar", self.ingest_calendar, self.cfg.calendar.poll_minutes),
                ("notion", self.sync_notion_calendar, self.cfg.notion.calendar_poll_minutes),
                ("workcal", self.sync_work_calendar, self.cfg.work_calendar.poll_minutes),
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
                    # If a forced check (e.g. "Check now") hit a transient error, mark LinkedIn as
                    # nudged so the next scheduled cycle retries it even before the poll interval expires.
                    if force and source == "linkedin":
                        self.db.kv_set("linkedin:check_now", True)
            # Chime now, before the slow drafting: filtered-out mail and our own follow-ups don't count. A
            # WhatsApp chat counts only once it is known to be about a job (after processing, below).
            summary["new_events"] = self.db.scalar(
                "SELECT COUNT(*) FROM events WHERE id > ? AND source NOT IN ('steward', 'whatsapp') AND "
                "status != 'ignored'", (last_event,)) or 0
            sound.new_events(self.cfg, summary["new_events"])
            # Anything you already handled yourself (on LinkedIn, by email, in your calendar) is closed now.
            summary["reconciled"] = await reconcile.run(self, hub, checked, summary["errors"])
            summary["cleaned"] = await self.cleanup_mail(hub, summary["errors"])
            summary["ingested"]["follow_ups"] = self.detect_follow_ups()
            try:
                summary["reprioritized"] = await self.reprioritize(hub)
            except (MCPToolError, OSError) as exc:
                summary["errors"].append(f"reprioritize: {exc}")
            processed, auto = await self.process_pending(hub, summary["errors"])
            summary["processed"], summary["auto_executed"] = processed, auto
            job_chats = self.db.scalar("SELECT COUNT(*) FROM events WHERE id > ? AND source = 'whatsapp' AND "
                                       "status NOT IN ('ignored', 'new')", (last_event,)) or 0
            if job_chats and not summary["new_events"]:
                sound.new_events(self.cfg, job_chats)
            summary["new_events"] += job_chats
            if checked.get("whatsapp", "").endswith(" new"):
                checked["whatsapp"] = f"{job_chats} new"  # personal chats are not even counted
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
        names = {"email": "email", "linkedin": "LinkedIn", "whatsapp": "WhatsApp", "calendar": "calendar",
                 "notion": "Notion events", "workcal": "work calendar"}
        idle = all(state in ("not due", "off") for state in checked.values()) and not (
            summary["processed"] or summary["auto_executed"] or summary.get("deferred_retried") or summary["errors"]
            or summary.get("reconciled") or summary.get("cleaned"))
        if idle and trigger == "scheduled":
            return  # the loop woke up but nothing was due: not a check worth a line
        parts = [f"{names[s]}: {state}" for s, state in checked.items()]
        for label, n in (("processed", summary["processed"]), ("handled automatically", summary["auto_executed"]),
                         ("reconciled", summary.get("reconciled", 0)), ("emails cleaned up", summary.get("cleaned", 0)),
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
            result["cleaned"] = await self.cleanup_mail(hub, result["errors"])
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
            await self.cleanup_mail(hub)
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
        async with MCPHub(self.cfg) as hub:
            await self.cleanup_mail(hub)
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
            await self.cleanup_mail(hub, errors)
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
                try:
                    sched = await self._schedule_from_plan(ctx, event, cls, plan, sched, warnings)
                except MCPToolError as exc:
                    sched = scheduler.unreadable(exc)
                    warnings.append("Your calendar could not be read, so no times were checked or booked: reconnect "
                                    "Google Calendar, then try again.")
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

    async def retry(self, draft_id: int) -> dict[str, Any]:
        """Work a waiting item out again from the message (after you reconnected what failed, say)."""
        old = self.db.get_draft(draft_id)
        if old is None or old["status"] not in ("pending", "approved", "edited", "rejected"):
            raise decisions.DecisionError("Only items still waiting for you can be tried again")
        self.db.update("drafts", draft_id, {"status": "superseded", "status_reason": "Tried again."})
        self.db.update("events", old["event_id"], {"status": "new", "attempts": 0, "error": None})
        errors: list[str] = []
        async with MCPHub(self.cfg) as hub:
            await self.process_pending(hub, errors)
        self.rescore()
        new = self.db.query("SELECT id FROM drafts WHERE event_id = ? AND id > ? AND status = 'pending' "
                            "ORDER BY id DESC LIMIT 1", (old["event_id"], draft_id))
        event = self.db.get_event(old["event_id"]) or {}
        if new:
            return {"draft_id": new[0]["id"], "status": "drafted"}
        if event.get("status") == "new":  # Claude is paused, or it failed once: the next check tries again
            return {"draft_id": None, "status": "queued", "error": event.get("error") or ""}
        return {"draft_id": None, "status": event.get("status") or "processed"}

    # -- review: follow up on quiet positions ------------------------------------------
    async def review_positions(self) -> dict[str, Any]:
        """Draft a follow-up (an update, and is it still open?) for every stale position waiting on nobody but the recruiter.

        A position is followed up when there was no contact for autonomy.follow_up_after_days, it is not closed, you were not turned down, no meeting or invitation for it
        is on your calendar, and nothing is waiting for you (a decision, a task, a message being read). Each
        follow-up is a decision item in the briefing: nothing is sent until you approve it."""
        now = utcnow()
        drafted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        created: list[tuple[dict[str, Any], int]] = []
        errors: list[str] = []
        async with MCPHub(self.cfg) as hub:
            if not hub.enabled("calendar"):
                raise decisions.DecisionError("Connect Google Calendar first (Setup step 5): the review checks it "
                                              "for meetings and invitations.")
            try:
                cal = (await hub.call("calendar", "list_events", {
                    "start": to_iso(now), "end": to_iso(now + timedelta(days=60))})).get("events", [])
            except MCPToolError as exc:
                raise decisions.DecisionError("Your calendar could not be read, so no position was reviewed. "
                                              f"Reconnect Google Calendar, then try again. ({exc})") from exc
            for opp in self.db.query("SELECT * FROM opportunities ORDER BY id"):
                name = " - ".join(x for x in (opp.get("company"), opp.get("role")) if x) or \
                    opp.get("recruiter_name") or f"Position {opp['id']}"
                why = self._review_blocker(opp, cal)
                event_id = None if why else self._insert_review_follow_up(opp)
                if not why and not event_id:
                    why = "No email, LinkedIn, or WhatsApp conversation with them to follow up in"
                if why:
                    skipped.append({"id": opp["id"], "position": name, "reason": why})
                else:
                    created.append(({**opp, "name": name}, event_id))
            if created:
                await self.process_pending(hub, errors)
        self.rescore()
        for opp, event_id in created:
            d = self.db.query_one("SELECT id FROM drafts WHERE event_id = ? AND status = 'pending' ORDER BY id DESC",
                                  (event_id,))
            if d:
                drafted.append({"id": opp["id"], "position": opp["name"], "draft_id": d["id"]})
            else:
                ev = self.db.get_event(event_id) or {}
                skipped.append({"id": opp["id"], "position": opp["name"],
                                "reason": f"The follow-up could not be written yet: {ev.get('error') or 'try again'}"})
        activity.record(self.db, "message", f"Position review: {len(drafted)} follow-up(s) drafted for your decision, "
                        f"{len(skipped)} position(s) left alone.")
        return {"drafted": drafted, "skipped": skipped, "errors": errors}

    def _review_blocker(self, opp: dict[str, Any], cal: list[dict[str, Any]]) -> str:
        """Why this position is not followed up now ('' when it should be)."""
        if opp["stage"] in CLOSED_STAGES:
            return f"Closed ({STAGES.get(opp['stage'], opp['stage'])})"
        if opp["stage"] == "offer":
            return "You have an offer: you handle it yourself"
        last = max(opp.get("last_inbound_at") or "", opp.get("last_outbound_at") or "")
        stale_days = self.cfg.autonomy.follow_up_after_days
        if last and days_between(last) < stale_days:
            return f"Not stale yet: last contact {fmt_local(last, self.cfg.profile.timezone)} " \
                   f"(stale after {stale_days} day{'' if stale_days == 1 else 's'})"
        for ev in self.db.query("SELECT classification_json, status FROM events WHERE opportunity_id = ?", (opp["id"],)):
            cls = loads(ev["classification_json"], {}) or {}
            if cls.get("stage_signal") == "rejection" or "rejection" in (cls.get("categories") or []):
                return "They turned you down"
            if ev["status"] == "new":
                return "A new message about it is still being read"
        email = (opp.get("recruiter_email") or "").lower()
        company = (opp.get("company") or "").strip().lower()
        ours = {(loads(x["result_json"], {}) or {}).get("id") for x in self.db.query(
            "SELECT x.result_json FROM executions x JOIN drafts d ON d.id = x.draft_id WHERE d.opportunity_id = ? "
            "AND x.status = 'success' AND x.action_type IN ('create_event', 'add_to_calendar')", (opp["id"],))} - {None}
        for e in cal:
            if e.get("my_response") == "declined":
                continue
            people = {(a.get("email") or "").lower() for a in e.get("attendees") or []}
            people.add(((e.get("organizer") or {}).get("email") or "").lower())
            title = (e.get("title") or "").lower()
            if e.get("id") in ours or (email and email in people) or (len(company) >= 3 and company in title):
                when = fmt_range(e.get("start", ""), e.get("end", ""), self.cfg.profile.timezone)
                if e.get("my_response") == "needsAction":
                    return f"An invitation is waiting for your answer: {e.get('title') or 'meeting'}, {when}"
                return f"A meeting is on your calendar: {e.get('title') or 'meeting'}, {when}"
        if self.db.query_one(f"SELECT 1 FROM drafts WHERE opportunity_id = ? AND status IN {WAITING_ON_YOU}",
                             (opp["id"],)):
            return "Something is waiting for you in the briefing"
        task = self.db.query_one("SELECT title FROM tasks WHERE opportunity_id = ? AND status IN ('open', 'suggested')",
                                 (opp["id"],))
        if task:
            return f"An open task: {task['title']}"
        return ""

    def _insert_review_follow_up(self, opp: dict[str, Any]) -> int | None:
        """The follow-up to draft, in their latest conversation (or the one you last wrote in)."""
        via = self._reply_target(opp)
        last = self.db.query_one(
            "SELECT o.* FROM outbound o JOIN opportunity_threads t ON t.thread_key = o.thread_key "
            "WHERE t.opportunity_id = ? ORDER BY o.id DESC LIMIT 1", (opp["id"],))
        if not via and not last:
            return None
        raw: dict[str, Any] = {"follow_up": True, "review": True, "instruction": REVIEW_INSTRUCTION}
        if via:
            raw.update({"channel": via["channel"], "reply_via": via})
            thread_key = via.get("thread_key") or ""
        else:
            raw.update({"channel": last["channel"], "last_outbound": last})
            thread_key = last["thread_key"]
        subject = (via or {}).get("subject") or (last or {}).get("subject") or ""
        return self._insert_event({
            "source": "steward", "external_id": f"review:{opp['id']}:{now_iso()}", "thread_key": thread_key,
            "sender_name": opp.get("recruiter_name") or "", "opportunity_id": opp["id"],
            "sender_address": opp.get("recruiter_email") or ((via or {}).get("to") or [""])[0],
            "subject": re.sub(r"^(re:\s*)+", "", subject, flags=re.I),
            "body_text": "(You asked Job Sourcer to check in on this position: ask for an update and whether it is "
                         "still open.)",
            "received_at": now_iso(), "raw_json": dumps(raw),
        })

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
                    "slots": await scheduler.offer(ctx, event, now, now + timedelta(days=sch.lookahead_days),
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
            why, filed = None, False
            if self.cfg.mail.file_application_acks and acknowledgments.is_application_ack(msg):
                # "Thank you for applying": nothing to answer or do. Read, filed, no AI call.
                why, filed = await self._file_application_ack(hub, msg)
            elif channels.is_linkedin_address(sender):
                # Never answered by email. A message notification means: read and answer it on LinkedIn, now.
                notice = channels.is_linkedin_message_notice(sender, subject, body)
                if notice and self.cfg.linkedin.backend != "disabled":
                    self.db.kv_set("linkedin:check_now", True)
                    # The notification names the exact conversation: read it even if the inbox list misses it.
                    notified = self.db.kv_get("linkedin:notified", {}) or {}
                    for tid in channels.linkedin_thread_ids(f"{subject}\n{body}"):
                        notified.setdefault(tid, {"name": msg.get("from_name", ""), "at": now_iso()})
                    self.db.kv_set("linkedin:notified", notified)
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
                **({"filed_at": now_iso()} if filed else {}),
            })
            if event_id:
                count += 1
                who = msg.get("from_name") or sender
                activity.record(self.db, "message", f"Email from {who}: {subject or '(no subject)'}"
                                + (f". {why}" if why else ""), event_id=event_id)
        self.db.kv_set("mail:last_uid", res.get("last_uid", since))
        return count

    async def _file_application_ack(self, hub: MCPHub, msg: dict[str, Any]) -> tuple[str, bool]:
        label = self.cfg.mail.applications_label
        try:
            await hub.call("mail", "file_message", {"uid": int(msg["uid"]), "folder": msg.get("folder", ""),
                                                    "label": label, "mark_read": True},
                           capability=ExecutorCapability())
            return f"Application acknowledgment: marked as read and moved to {label} (nothing to answer or do).", True
        except (MCPToolError, KeyError, ValueError) as exc:
            return (f"Application acknowledgment (nothing to answer or do), but it could not be moved to {label}: "
                    f"{exc}"), False

    async def cleanup_mail(self, hub: MCPHub, errors: list[str] | None = None) -> int:
        """Handled items' emails leave the inbox: marked as read and moved to the Jobs label."""
        try:
            return await mail_cleanup.sweep(self, hub)
        except (MCPToolError, OSError) as exc:
            if errors is not None:
                errors.append(f"mail cleanup: {exc}")
            return 0

    async def ingest_linkedin(self, hub: MCPHub) -> int:
        cursor = self.db.kv_get("linkedin:since", "")
        # Re-read a window before the newest message seen: a thread can appear in the inbox list after a newer
        # message in another thread moved the cursor on. Events are de-duplicated, so nothing is read twice.
        start = parse_iso(cursor)
        since = to_iso(start - timedelta(hours=self.cfg.linkedin.recheck_hours)) if start else ""
        res = await hub.call("linkedin", "list_conversations", {"since": since, "limit": 50})
        convs = list(res.get("conversations", []))
        convs += await self._notified_linkedin_threads(hub, {c["id"].rstrip("=") for c in convs})
        count = 0
        newest = cursor
        for conv in convs:
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

    async def _notified_linkedin_threads(self, hub: MCPHub, listed: set[str]) -> list[dict[str, Any]]:
        """Conversations a LinkedIn notification email pointed to, read directly (however old their messages are).

        One that cannot be read now is tried again on the next check, for up to 7 days."""
        notified: dict[str, Any] = self.db.kv_get("linkedin:notified", {}) or {}
        out = []
        for tid, info in list(notified.items()):
            if tid.rstrip("=") in listed:
                notified.pop(tid)
                continue
            try:
                conv = await hub.call("linkedin", "get_conversation", {"conversation_id": tid, "limit": 20})
            except MCPToolError as exc:
                if (parse_iso(info.get("at")) or utcnow()) < utcnow() - timedelta(days=7):
                    notified.pop(tid)
                log.warning("notified LinkedIn thread %s not read yet: %s", tid, exc)
                continue
            conv["id"] = conv.get("id") or tid
            conv["participant_name"] = conv.get("participant_name") or info.get("name", "")
            out.append(conv)
            notified.pop(tid)
        self.db.kv_set("linkedin:notified", notified)
        return out

    async def ingest_whatsapp(self, hub: MCPHub) -> int:
        """New messages in one-to-one WhatsApp chats. A chat that is not about a job is ignored completely:
        nothing of it is stored or logged (the classifier gets only chats that look job-related)."""
        since = self.db.kv_get("whatsapp:since", "")
        res = await hub.call("whatsapp", "list_conversations", {"since": since, "limit": 30})
        count = 0
        newest = since
        for conv in res.get("conversations", []):
            msgs = conv.get("messages", [])
            last_out = max((i for i, m in enumerate(msgs) if m["from"] == "me"), default=-1)
            new_in = [m for m in msgs[last_out + 1:] if m["from"] == "them"]
            if not new_in:
                continue
            last = new_in[-1]
            if not newest or (last.get("sent_at") or "") > newest:
                newest = last.get("sent_at") or newest
            if conv.get("is_group") or not self._maybe_job_chat(conv, msgs):
                continue
            event_id = self._insert_event({
                "source": "whatsapp", "external_id": f"{conv['id']}:{last['id']}", "thread_key": conv["id"],
                "sender_name": conv.get("participant_name", ""),
                "sender_address": conv.get("phone") or f"whatsapp:{conv['id']}",
                "subject": "", "body_text": "\n\n".join(m["text"] for m in new_in),
                "received_at": last.get("sent_at") or now_iso(),
                "raw_json": dumps({"conversation_id": conv["id"], "phone": conv.get("phone", ""),
                                   "new_messages": len(new_in), "history": msgs[: max(last_out + 1, 0)]}),
            })
            count += 1 if event_id else 0
        if newest:
            self.db.kv_set("whatsapp:since", newest)
        return count

    def _maybe_job_chat(self, conv: dict[str, Any], msgs: list[dict[str, Any]]) -> bool:
        """Worth classifying: a chat already tied to an opportunity, or one whose recent messages mention a job."""
        if self.db.query_one("SELECT 1 FROM opportunity_threads WHERE source = 'whatsapp' AND thread_key = ?",
                             (conv["id"],)):
            return True
        number = pipeline.digits(conv.get("phone", ""))
        if number and any(pipeline.digits(o["recruiter_phone"]) == number for o in self.db.query(
                "SELECT recruiter_phone FROM opportunities WHERE ifnull(recruiter_phone, '') != ''")):
            return True
        text = "\n".join(m.get("text", "") for m in msgs[-12:])
        return bool(PREFILTER.search(text) or JOB_WORDS_ES.search(text))

    async def sync_work_calendar(self, hub: MCPHub) -> int:
        """Mirror your work (Microsoft 365) calendar onto your personal calendar: new meetings are added, moved or
        renamed ones follow, and cancelled ones are removed (only copies Job Sourcer made). Returns the changes."""
        w = self.cfg.work_calendar
        if not (w.enabled and hub.enabled("calendar")):
            raise IntegrationDisabled("work calendar sync is off")
        res = await hub.call("workcal", "events", {"days": w.lookahead_days}, timeout=180)
        events = res.get("events", [])
        copies: dict[str, dict[str, Any]] = self.db.kv_get("work:calendar_copies", {}) or {}
        now, cap = utcnow(), ExecutorCapability()
        added = moved = removed = 0
        for e in events:
            known = copies.get(e["uid"]) or {}
            if known and (known.get("start"), known.get("end"), known.get("title")) == (e["start"], e["end"], e["title"]):
                continue  # unchanged since the last check (or you deleted the copy: left out)
            description = "Copied from your work calendar by Job Sourcer." + "".join(
                f"\n{label}: {e[k]}" for k, label in (("organizer", "Organizer"), ("join_url", "Join"))
                if e.get(k))
            out = await hub.call("calendar", "sync_copy", {
                "uid": e["uid"], "title": e["title"], "start": e["start"], "end": e["end"],
                "location": e.get("location", ""), "description": description,
                "event_id": known.get("id", "")}, capability=cap)
            copies[e["uid"]] = {"id": out["id"], "start": e["start"], "end": e["end"], "title": e["title"]}
            added += out["status"] == "added"
            moved += out["status"] == "updated"
        current = {e["uid"] for e in events}
        future = [u for u, c in copies.items() if u not in current and (parse_iso(c.get("start")) or now) > now]
        if future and not events and len(future) > 3:
            # An empty calendar where meetings were a moment ago is more likely a broken link than a free month.
            activity.record(self.db, "calendar", "Work calendar came back empty: no copies were removed. Check the "
                            "link or sign-in in Setup.", "warn")
        else:
            for uid in future:
                await hub.call("calendar", "remove_copy", {"event_id": copies[uid]["id"], "uid": uid}, capability=cap)
                copies.pop(uid)
                removed += 1
        for uid in [u for u, c in copies.items() if (parse_iso(c.get("end")) or now) < now - timedelta(days=2)]:
            copies.pop(uid)  # long over: no need to remember
        self.db.kv_set("work:calendar_copies", copies)
        if added or moved or removed:
            activity.record(self.db, "calendar", f"Work calendar: {added} meeting(s) added, {moved} moved, "
                            f"{removed} cancelled ones removed from your calendar.")
        return added + moved + removed

    async def sync_notion_calendar(self, hub: MCPHub) -> int:
        """Rows with a date and time in your Notion workspaces go on your calendar, and follow them when they move.

        Every workspace whose integration token is set (notion.token_env plus notion.extra_token_envs) is read;
        only databases shared with that integration are visible. Nothing is ever removed from the calendar.
        Returns how many entries were added or moved."""
        n = self.cfg.notion
        if not (n.calendar_sync and hub.enabled("notion") and hub.enabled("calendar")):
            raise IntegrationDisabled("Notion calendar sync is off")
        tz_name, changed = self.cfg.profile.timezone, 0
        synced: dict[str, str] = self.db.kv_get("notion:calendar_copies", {}) or {}  # Notion entry uid -> event id
        for token_env in [n.token_env, *n.extra_token_envs]:
            if not os.environ.get(token_env):
                continue
            res = await hub.call("notion", "scheduled_entries", {
                "days": n.calendar_lookahead_days, "token_env": token_env, "exclude_database_id": n.database_id,
                "default_minutes": n.calendar_default_minutes})
            for e in res.get("entries", []):
                where = " / ".join(x for x in (e["workspace"], e["database"]) if x)
                out = await hub.call("calendar", "sync_copy", {
                    "uid": e["uid"], "title": e["title"], "start": e["start"], "end": e["end"],
                    "description": f"From Notion ({where}): {e['url']}", "event_id": synced.get(e["uid"], "")},
                    capability=ExecutorCapability())
                if out["status"] != "removed":
                    synced[e["uid"]] = out["id"]
                when = fmt_range(e["start"], e["end"], tz_name)
                if out["status"] == "added":
                    clash = f" It overlaps {', '.join(out['overlaps'])}." if out.get("overlaps") else ""
                    activity.record(self.db, "calendar", f"From Notion ({where}): {e['title']}, {when} "
                                    f"added to your calendar.{clash}", "warn" if clash else "info")
                elif out["status"] == "updated":
                    activity.record(self.db, "calendar", f"From Notion ({where}): {e['title']} moved on your "
                                    f"calendar to {when}.")
                changed += out["status"] in ("added", "updated")
        self.db.kv_set("notion:calendar_copies", synced)
        return changed

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
                                f": {inv.get('title', '')} ({when[6:]}). It is on your calendar"
                                + ("; it is accepted for you." if self.cfg.calendar.confirm_invites else
                                   "; whether to answer Yes/No is up to you."), event_id=event_id)
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
        if event["source"] in ("linkedin", "whatsapp"):
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
    def _received_label(event: dict[str, Any], cls: dict[str, Any], meeting_change: bool, call: bool) -> str:
        raw = event.get("raw") or {}
        if call:
            return "Missed call received"
        if meeting_change:
            return f"Meeting {raw.get('change') or 'changed'}"
        if event["source"] == "calendar" or raw.get("invites") or "calendar_invite" in (cls.get("categories") or []):
            return "Invitation received"
        what = {"email": "an email", "linkedin": "a LinkedIn message", "whatsapp": "a WhatsApp message"}
        return f"Just received {what.get(event['source'], 'a message')}"

    @staticmethod
    def _fixed_classification(category: str, summary: str, event: dict[str, Any], needs_reply: bool) -> dict[str, Any]:
        """Events Job Sourcer generated itself need no LLM triage."""
        return {"is_recruiter_related": True, "needs_reply": needs_reply, "summary": summary,
                "company": "", "role": "", "recruiter_name": event.get("sender_name", ""), "phone_number": "",
                "location": "", "categories": [category], "primary_category": category, "questions": [],
                "technologies": [], "proposed_times": [], "meeting_link": "", "stage_signal": "none",
                "urgency": "normal", "suspicious": False, "risk_level": risk_of([category])}

    def _reply_target(self, opp: dict[str, Any] | None) -> dict[str, Any] | None:
        """Where to answer someone who reached you by phone: their latest email, LinkedIn, or WhatsApp thread."""
        if not opp:
            return None
        ev = next((e for e in self.db.query(
            "SELECT * FROM events WHERE opportunity_id = ? AND source IN ('email', 'linkedin', 'whatsapp') "
            "ORDER BY received_at DESC",
            (opp["id"],)) if not (e["source"] == "email" and channels.is_linkedin_address(e["sender_address"]))), None)
        if ev and ev["source"] in ("linkedin", "whatsapp"):
            return {"channel": ev["source"], "conversation_id": ev["thread_key"], "recipient": ev["sender_name"],
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
            cls = self._fixed_classification(
                "follow_up", "Checking in: asking for an update and whether the position is still open"
                if raw.get("review") else "Follow-up on a quiet thread", event, True)
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
            if event["source"] == "whatsapp":
                # A personal chat: nothing of it is kept (only the id, so it is not read again).
                self.db.update("events", event["id"], {
                    "status": "ignored", "processed_at": now_iso(), "error": "Not about a job: ignored.",
                    "sender_name": "", "sender_address": "", "body_text": "", "raw_json": "{}",
                    "classification_json": None})
                return "ignored"
            if event["source"] == "calendar" and not meeting_change and self.cfg.calendar.confirm_invites:
                return await self._accept_calendar_invite(ctx, event, cls, None)
            # Not about a job, but an invitation in it still goes on your calendar (its email stays where it is).
            await self._add_invites(ctx, event, cls, None, job=False)
            self.db.update("events", event["id"], {"status": "ignored", "processed_at": now_iso(),
                                                   "error": "Not related to your job search."})
            return "ignored"
        if event["source"] == "whatsapp":
            if not event.get("sender_name") and cls.get("recruiter_name"):
                # A hidden-number chat (…@lid) often comes without a contact name: use the one in the messages.
                event["sender_name"] = cls["recruiter_name"]
                self.db.update("events", event["id"], {"sender_name": event["sender_name"]})
            activity.record(self.db, "message", f"WhatsApp message from {event.get('sender_name') or 'someone'} "
                            f"({raw.get('new_messages', 1)} new): {cls.get('summary') or 'about a job'}",
                            event_id=event["id"])

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
        if opp and not follow_up:
            pipeline.note(self.db, opp["id"], self._received_label(event, cls, meeting_change, call))
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
        if self._plain_rejection(event, cls):
            return await self._close_rejected(ctx.hub, event, cls, opp)

        # A newer message in the same thread makes any draft that has not gone out stale (undecided, waiting,
        # deferred, or failed): re-executing it later would answer an old message.
        self.db.execute(
            "UPDATE drafts SET status = 'superseded', status_reason = 'A newer message arrived in this thread.', "
            "closed_at = ? WHERE status IN ('pending', 'approved', 'edited', 'rejected', 'queued', 'deferred', 'failed') "
            "AND event_id IN (SELECT id FROM events WHERE thread_key = ? AND source = ? AND id != ?)",
            (now_iso(), event.get("thread_key") or "-", event["source"], event["id"]))

        if event["source"] == "calendar" and not meeting_change and self.cfg.calendar.confirm_invites:
            return await self._accept_calendar_invite(ctx, event, cls, opp)
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
            # The "I will attend" reply already went out: write more only if they asked something else.
            needs_text = cls["needs_reply"] and not (cls.get("invite_confirmed") and not cls.get("questions"))
        if not needs_text and not sched.get("booking") and not sched.get("rsvp"):
            self.db.update("events", event["id"], {"status": "info", "processed_at": now_iso()})
            return "info"

        prefs = decisions.active_preferences(self.db, cls["categories"], (opp or {}).get("company") or cls.get("company", ""),
                                             event.get("sender_address", ""))
        examples = fewshot.retrieve(self.ledger, event, cls)
        if needs_text:
            drafted = await drafter.draft(ctx, event, thread, cls, evidence, sched, opp, prefs, examples,
                                          instruction=raw.get("instruction", ""))
            review = await drafter.critique(ctx, event, cls, drafted, evidence, sched, opp)
        else:
            drafted = {"reply_subject": "", "reply_body": "", "answered_questions": [], "unanswered_questions": [],
                       "commitments": [], "next_step": "", "confidence": 0.95,
                       "rationale": "Calendar-only action; nothing to write."}
            review = {"job_advancement_score": 0.85, "confidence": 0.95, "verdict": "send", "issues": [], "risks": []}

        actions = self.build_actions(event, cls, drafted, sched, opp)
        decision = route(self.cfg, self.db, event=event, cls=cls, evidence=evidence, sched=sched, drafted=drafted,
                         review=review, opp=opp)
        if raw.get("review"):  # a follow-up you asked for always waits for your decision
            decision.route = "ask"
            decision.reasons.insert(0, "Follow-up from your position review: approve it to send.")
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

    @staticmethod
    def _plain_rejection(event: dict[str, Any], cls: dict[str, Any]) -> bool:
        """An email that only turns you down: no question, request, time, or other topic in it."""
        cats = set(cls.get("categories") or [])
        return (event["source"] == "email" and "rejection" in cats | {cls.get("stage_signal")}
                and cats <= {"rejection", "status_update", "other"} and not cls.get("questions")
                and not cls.get("candidate_tasks") and not cls.get("proposed_times") and not cls.get("suspicious"))

    async def _close_rejected(self, hub: MCPHub, event: dict[str, Any], cls: dict[str, Any],
                              opp: dict[str, Any] | None) -> str:
        """Close the opportunity (Notion follows in this check's sync) and move the email to Trash. No reply."""
        who = (opp or {}).get("company") or cls.get("company") or event.get("sender_name") or event.get("sender_address")
        if opp:
            # The email is the newest news: it wins over a stage just pulled from Notion.
            pipeline.set_stage(self.db, opp["id"], "closed_lost", force=True)
            self.db.update("opportunities", opp["id"], {"next_action": "Turned down: not moving forward"})
            pipeline.note(self.db, opp["id"], "Rejected: they are not moving forward")
        done = [f"Rejection from {who}: pipeline set to Closed (lost)"
                + (" (Notion is updated in this check)" if opp and hub.enabled("notion") else "")]
        raw, trashed = event["raw"], None
        if self.cfg.mail.delete_rejections and raw.get("uid") is not None:
            try:
                trashed = await hub.call("mail", "trash_message", {"uid": int(raw["uid"]), "folder": raw.get("folder", "")},
                                         capability=ExecutorCapability())
                done.append(f"the email was moved to {trashed.get('folder') or 'Trash'}")
            except (MCPToolError, ValueError) as exc:
                done.append(f"the email could not be moved to Trash: {exc}")
        elif not self.cfg.mail.delete_rejections:
            done.append("the email was kept (mail.delete_rejections is off)")
        note = "; ".join(done) + ". No reply drafted."
        # Anything still waiting to answer them (this thread, or this opportunity on any channel) is moot now.
        for d in self.db.query(
                "SELECT * FROM drafts WHERE status IN ('pending', 'approved', 'edited', 'rejected', 'queued', 'deferred', "
                "'failed') AND (opportunity_id = ? OR event_id IN (SELECT id FROM events WHERE thread_key = ? AND "
                "source = 'email' AND id != ?))", ((opp or {}).get("id") or -1, event.get("thread_key") or "-",
                                                   event["id"])):
            reconcile.close(self, d, f"{who} turned you down in a newer email, so this is no longer needed. "
                                     "Nothing was sent.")
        self.db.update("events", event["id"], {"status": "info", "processed_at": now_iso(), "error": note,
                                               **({"filed_at": now_iso()} if trashed else {})})
        self.ledger.append("rejection", {"company": who, "subject": event.get("subject", ""),
                                         "sender": event.get("sender_address", ""), "summary": cls.get("summary", ""),
                                         "opportunity_id": (opp or {}).get("id"), "stage": "closed_lost",
                                         "email": "trashed" if trashed else "kept"}, event_id=event["id"])
        activity.record(self.db, "message", note, event_id=event["id"])
        return "info"

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
                           opp: dict[str, Any] | None, job: bool = True) -> list[str]:
        """Any meeting invitation in an email goes on your calendar right away, without asking, whoever sent it.

        Added as is (no reply to the organizer, nobody invited) and never twice. Returns the times added."""
        raw = event["raw"]
        found = [i for i in raw.get("invites") or [] if i.get("method") != "cancel"]
        if event["source"] != "email" or not (found or raw.get("add_to_calendar")):
            return []
        who = event.get("sender_name") or event.get("sender_address")
        if not found:
            if raw.get("invites"):  # only cancellations: there is nothing to add
                return []
            activity.record(self.db, "calendar", f"Email from {who} offers 'Add to Calendar', but its date and time "
                            "could not be read (no invitation file, and the link carries no time). Add it yourself; "
                            "the message is in your briefing.", "warn", event_id=event["id"])
            return []
        if cls.get("suspicious"):
            activity.record(self.db, "calendar", f"Invitation from {who} looks suspicious; adding it to your calendar "
                            "anyway. Check it before you join.", "warn", event_id=event["id"])
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
        # A job invitation is also answered: "I will attend". Once per meeting, never to a no-reply address.
        confirmed = self.db.kv_get("invites:confirmed", []) or []
        new = [i for i in found if (i["uid"] or f"{i['start']}|{i['title']}") not in confirmed]
        reply = []
        if job and self.cfg.calendar.confirm_invites and new and not cls.get("suspicious"):
            body = _attendance_note(event, new, self.cfg.profile.name, tz_name)
            reply = [a for a in self.build_actions(event, cls, {"reply_body": body}, {}, opp)
                     if a["type"] == "send_email"]
        if reply:
            actions += reply
            self.db.kv_set("invites:confirmed", confirmed + [i["uid"] or f"{i['start']}|{i['title']}" for i in new])
            cls["invite_confirmed"] = True
        elif job and new and cls.get("suspicious"):
            activity.record(self.db, "calendar", f"Invitation from {who} looks suspicious: added to your calendar, "
                            "but no 'I will attend' reply was sent. Answer it yourself if it is real.", "warn",
                            event_id=event["id"])
        draft_id = self.db.insert("drafts", {
            "event_id": event["id"], "opportunity_id": opp["id"] if opp else None, "channel": "calendar",
            "summary": f"Added to your calendar{' and confirmed you will attend' if reply else ''}: {'; '.join(labels)}",
            "actions_json": dumps(actions),
            "analysis_json": dumps({"classification": cls}), "confidence": 1.0, "job_advancement": 1.0,
            "routing_band": "HIGH", "risk_level": "low", "priority": "normal", "route": "auto_approve",
            "route_reasons_json": dumps(["Meeting invitations go on your calendar automatically"
                                         + (" and are answered that you will attend." if reply else
                                            ", without a reply to the organizer.")]),
            "status": "queued", "created_at": now_iso(), "decided_at": now_iso(),
            **({} if job else {"cleaned_at": now_iso()}),  # a personal invitation is never filed under Jobs
        })
        self.ledger.append("auto_execution", {
            **decisions.ledger_payload(self.db, self.db.get_draft(draft_id), "auto", actions),
            "rule_id": None, "tier": "standing_instruction",
            "instruction": "Add every meeting invitation to my calendar without asking"
                           + (", and reply that I will attend." if reply else " (no reply)."),
        }, event_id=event["id"], draft_id=draft_id)
        await Executor(self.cfg, self.db, self.ledger, ctx.hub).execute_draft(draft_id)
        return [fmt_range(i["start"], i["end"], tz_name) for i in found]

    async def _accept_calendar_invite(self, ctx: AgentContext, event: dict[str, Any], cls: dict[str, Any],
                                      opp: dict[str, Any] | None) -> str:
        """A calendar invitation is accepted right away, without asking (a conflict is flagged, not a blocker)."""
        raw = event["raw"]
        who = event.get("sender_name") or event.get("sender_address") or "someone"
        when = fmt_range(raw.get("start", ""), raw.get("end", ""), self.cfg.profile.timezone)
        if cls.get("suspicious"):
            activity.record(self.db, "calendar", f"Invitation from {who} ({when}) looks suspicious: it stays on your "
                            "calendar, but it was not accepted. Check it and answer it yourself.", "warn",
                            event_id=event["id"])
            self.db.update("events", event["id"], {"status": "info", "processed_at": now_iso()})
            return "info"
        try:
            check = await ctx.hub.call("calendar", "check_availability",
                                       {"start": raw["start"], "end": raw["end"], "buffer_minutes": 0})
            clash = [c.get("title", "busy") for c in check.get("conflicts", []) if c.get("id") != raw.get("id")]
        except (MCPToolError, KeyError):
            clash = []
        actions = [{"type": "rsvp_event", "payload": {"event_id": raw.get("id"), "response": "accepted",
                                                      "despite_conflicts": True}}]
        draft_id = self.db.insert("drafts", {
            "event_id": event["id"], "opportunity_id": opp["id"] if opp else None, "channel": "calendar",
            "summary": f"Accepted: {event.get('subject') or 'meeting'}, {when}", "actions_json": dumps(actions),
            "analysis_json": dumps({"classification": cls}), "confidence": 1.0, "job_advancement": 1.0,
            "routing_band": "HIGH", "risk_level": "low", "priority": "normal", "route": "auto_approve",
            "route_reasons_json": dumps(["Calendar invitations are accepted automatically."]),
            "status": "queued", "created_at": now_iso(), "decided_at": now_iso(),
            **({} if cls.get("is_recruiter_related") else {"cleaned_at": now_iso()}),
        })
        self.ledger.append("auto_execution", {
            **decisions.ledger_payload(self.db, self.db.get_draft(draft_id), "auto", actions),
            "rule_id": None, "tier": "standing_instruction",
            "instruction": "Accept every calendar invitation without asking.",
        }, event_id=event["id"], draft_id=draft_id)
        if clash:
            activity.record(self.db, "calendar", f"Accepted {event.get('subject') or 'the invitation'} ({when}) "
                            f"although it overlaps {', '.join(clash)}. Sort out the double booking.", "warn",
                            event_id=event["id"], draft_id=draft_id)
        await Executor(self.cfg, self.db, self.ledger, ctx.hub).execute_draft(draft_id)
        self.db.update("events", event["id"], {"status": "processed", "processed_at": now_iso()})
        return "auto"

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
        if via and via["channel"] in ("linkedin", "whatsapp"):
            actions.append({"type": f"send_{via['channel']}", "payload": {
                "conversation_id": via["conversation_id"], "text": body, "recipient": via.get("recipient", "")}})
            return actions
        if via:
            actions.append({"type": "send_email", "payload": {
                "to": via["to"], "cc": [], "subject": via.get("subject") or drafted.get("reply_subject") or "",
                "body": body, "in_reply_to": via.get("in_reply_to", ""), "references": via.get("references", [])}})
            return actions
        channel = event["source"]
        if channel == "steward":
            channel = raw.get("channel", "email")
        if channel in ("linkedin", "whatsapp"):
            conv = raw.get("conversation_id") or event.get("thread_key")
            if raw.get("last_outbound"):
                conv = raw["last_outbound"].get("thread_key") or conv
            actions.append({"type": f"send_{channel}", "payload": {"conversation_id": conv, "text": body,
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
            "route_reasons_json": dumps([f"They called you. Call back {number}: no email, LinkedIn, or WhatsApp "
                                         "thread is known for them yet, so Job Sourcer cannot reply in writing."]),
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
        if not hub.enabled("notion") or not self.cfg.notion.database_id:
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
