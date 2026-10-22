"""The executor: the only component that acts on the outside world.

- Runs only approved drafts (by you, or by a rule you activated).
- Exactly-once: every action has a deterministic idempotency key; a retry never
  re-sends something that succeeded, and an interrupted attempt is never retried
  blindly (it is marked `unknown` for you to check).
- Re-checks the calendar immediately before booking or accepting; a conflict
  that appeared since drafting sends the message back for a fresh draft.
- LinkedIn sends are human-paced; sends that are not allowed yet are deferred and
  retried by the background loop.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from steward import activity, channels, decisions, pipeline
from steward.config import Config
from steward.db import Database, dumps, loads
from steward.ledger import Ledger
from steward.mcp_hub import ExecutorCapability, MCPHub
from steward.timeutil import fmt_local, now_iso, parse_iso, to_iso, utcnow

ACTION_LABELS = {"send_email": "email reply", "send_linkedin": "LinkedIn reply", "create_event": "meeting booking",
                 "rsvp_event": "invitation answer", "add_to_calendar": "add to calendar"}


class SlotConflict(RuntimeError):
    pass


def idempotency_key(draft_id: int, index: int, action: dict[str, Any]) -> str:
    material = f"{draft_id}|{index}|{action['type']}|{dumps(action['payload'])}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Executor:
    def __init__(self, cfg: Config, db: Database, ledger: Ledger, hub: MCPHub):
        self.cfg, self.db, self.ledger, self.hub = cfg, db, ledger, hub
        self._cap = ExecutorCapability()

    # -- batch ("go") -----------------------------------------------------------
    async def run_batch(self) -> dict[str, Any]:
        committed = {"approved": 0, "rejected": 0}
        for draft in self.db.query(
                "SELECT * FROM drafts WHERE status IN ('approved', 'edited', 'rejected') ORDER BY id"):
            status = decisions.commit(self.cfg, self.db, self.ledger, draft)
            committed["approved" if status == "queued" else "rejected"] += 1
        results = await self.run_queue()
        stamp = now_iso()
        self.db.execute("UPDATE drafts SET acknowledged_at = ? WHERE status = 'executed' AND route != 'ask' "
                        "AND acknowledged_at IS NULL", (stamp,))
        self.db.execute("UPDATE proposals SET status = 'seen' WHERE status = 'info'")
        self.db.kv_set("last_go_at", stamp)
        return {"committed": committed, **results}

    async def run_queue(self, include_deferred_only: bool = False) -> dict[str, Any]:
        """Execute queued drafts and deferred drafts that are due."""
        statuses = "('deferred')" if include_deferred_only else "('queued', 'deferred')"
        out = {"executed": 0, "deferred": 0, "failed": 0, "superseded": 0, "details": []}
        for draft in self.db.query(f"SELECT * FROM drafts WHERE status IN {statuses} ORDER BY id"):
            if draft["status"] == "deferred" and not self._due(draft["id"]):
                continue
            res = await self.execute_draft(draft["id"])
            out[{"executed": "executed", "deferred": "deferred", "superseded": "superseded"}.get(res["status"], "failed")] += 1
            out["details"].append(res)
        return out

    def _due(self, draft_id: int) -> bool:
        row = self.db.query_one("SELECT next_attempt_at FROM executions WHERE draft_id = ? AND status = 'deferred' "
                                "ORDER BY id DESC LIMIT 1", (draft_id,))
        nxt = parse_iso(row["next_attempt_at"]) if row else None
        return nxt is None or nxt <= utcnow()

    # -- one draft --------------------------------------------------------------
    async def execute_draft(self, draft_id: int, force: bool = False) -> dict[str, Any]:
        """Run a draft's actions. `force` (your Execute button) skips LinkedIn's business hours and pacing."""
        draft = self.db.get_draft(draft_id)
        if draft is None:
            return {"draft_id": draft_id, "status": "failed", "error": "missing draft"}
        event = self.db.get_event(draft["event_id"]) or {}
        actions = loads(draft["final_actions_json"], None) or loads(draft["actions_json"], [])
        self.db.update("drafts", draft_id, {"status": "executing"})
        outcomes = []
        final_status = "executed"
        reason = ""
        for index, action in enumerate(actions):
            if action.get("skip"):
                outcomes.append({"index": index, "type": action["type"], "status": "skipped"})
                continue
            key = idempotency_key(draft_id, index, action)
            row = self.db.query_one("SELECT * FROM executions WHERE idempotency_key = ?", (key,))
            if row and row["status"] in ("success", "handed_off"):
                outcomes.append({"index": index, "type": action["type"], "status": "already_done"})
                continue
            if row and row["status"] in ("in_flight", "unknown"):
                self.db.update("executions", row["id"], {"status": "unknown", "updated_at": now_iso()})
                final_status, reason = "failed", (
                    f"A previous attempt at {action['type']} was interrupted; check whether it went out before retrying.")
                break
            if row:
                self.db.update("executions", row["id"], {"status": "in_flight", "attempts": row["attempts"] + 1,
                                                         "updated_at": now_iso()})
                exec_id = row["id"]
            else:
                exec_id = self.db.insert("executions", {
                    "draft_id": draft_id, "action_index": index, "action_type": action["type"],
                    "idempotency_key": key, "status": "in_flight", "created_at": now_iso(), "updated_at": now_iso(),
                })
            try:
                result = await self._run(action, draft, event, force)
            except SlotConflict as exc:
                self.db.update("executions", exec_id, {"status": "failed", "error": str(exc), "updated_at": now_iso()})
                final_status, reason = "superseded", str(exc)
                outcomes.append({"index": index, "type": action["type"], "status": "conflict", "error": str(exc)})
                break
            except Exception as exc:  # noqa: BLE001 - every failure is recorded and surfaced
                self.db.update("executions", exec_id, {"status": "failed", "error": str(exc)[:1000],
                                                       "updated_at": now_iso()})
                final_status, reason = "failed", f"{action['type']} failed: {exc}"
                outcomes.append({"index": index, "type": action["type"], "status": "failed", "error": str(exc)})
                break
            status = result.get("status", "sent")
            if status == "deferred":
                retry = int(result.get("retry_after_seconds", 300))
                self.db.update("executions", exec_id, {
                    "status": "deferred", "result_json": dumps(result), "updated_at": now_iso(),
                    "next_attempt_at": to_iso(utcnow() + timedelta(seconds=retry)),
                })
                final_status, reason = "deferred", f"LinkedIn pacing: {result.get('reason', 'waiting')}"
                outcomes.append({"index": index, "type": action["type"], "status": "deferred", "retry_in": retry})
                break
            self.db.update("executions", exec_id, {
                "status": "handed_off" if status == "queued" else "success",
                "result_json": dumps(result), "updated_at": now_iso(),
            })
            self._after_success(action, result, draft, event)
            outcomes.append({"index": index, "type": action["type"], "status": status, "result": result})

        if final_status == "executed" and any(o["status"] == "queued" for o in outcomes):
            reason = ("LinkedIn reply handed to the linkedin-toolkit queue: approve it in the browser extension popup, "
                      "or turn on its Autopilot so replies you approved here go out at its human pace.")
        updates: dict[str, Any] = {"status": final_status, "status_reason": reason or None}
        if final_status == "executed":
            updates["executed_at"] = now_iso()
            if draft["matched_rule_id"] and draft["route"] != "ask":
                self.db.execute("UPDATE rules SET times_executed = times_executed + 1 WHERE id = ?",
                                (draft["matched_rule_id"],))
            self.db.update("events", draft["event_id"], {"status": "processed"})
        elif final_status == "superseded":
            # The calendar changed since drafting: re-process the message with fresh availability.
            self.db.update("events", draft["event_id"], {"status": "new", "attempts": 0})
        self.db.update("drafts", draft_id, updates)
        if any(o["status"] not in ("already_done", "skipped") for o in outcomes):
            self.ledger.append("execution", {"status": final_status, "reason": reason, "outcomes": outcomes,
                                             "route": draft["route"], "forced": force},
                               event_id=draft["event_id"], draft_id=draft_id, rule_id=draft["matched_rule_id"])
            self._log(draft, final_status, reason, outcomes, force)
        return {"draft_id": draft_id, "status": final_status, "reason": reason, "outcomes": outcomes}

    def _log(self, draft: dict[str, Any], status: str, reason: str, outcomes: list[dict[str, Any]], force: bool) -> None:
        done = [ACTION_LABELS.get(o["type"], o["type"]) for o in outcomes if o["status"] in ("sent", "ok", "added")]
        queued = [o for o in outcomes if o["status"] == "queued"]
        exists = [o for o in outcomes if o["status"] == "exists"]
        what = draft["summary"]
        how = " (you pressed Execute: business hours and pacing skipped)" if force else ""
        if status == "executed":
            overlaps = sorted({t for o in outcomes for t in (o.get("result") or {}).get("overlaps") or []})
            parts = ([f"done: {', '.join(done)}"] if done else []) + \
                    (["LinkedIn reply handed to the linkedin-toolkit queue (approve it in the extension)"] if queued else []) + \
                    (["already on your calendar, not added again"] if exists else []) + \
                    ([f"it overlaps {', '.join(overlaps)} on your calendar"] if overlaps else [])
            activity.record(self.db, "action", f"{what}: {'; '.join(parts) or 'nothing to do'}{how}",
                            "warn" if overlaps else "info", draft_id=draft["id"])
        elif status == "deferred":
            nxt = next((o.get("retry_in") for o in outcomes if o["status"] == "deferred"), 0) or 0
            when = fmt_local(utcnow() + timedelta(seconds=nxt), self.cfg.profile.timezone)
            activity.record(self.db, "action", f"{what}: deferred, {reason}. Next try {when}, or press Execute in "
                            "the Queue tab.", "warn", draft_id=draft["id"])
        else:
            activity.record(self.db, "action", f"{what}: {status}. {reason}{how}",
                            "warn" if status == "superseded" else "error", draft_id=draft["id"])

    async def _run(self, action: dict[str, Any], draft: dict[str, Any], event: dict[str, Any],
                   force: bool = False) -> dict[str, Any]:
        p = action["payload"]
        kind = action["type"]
        gaps = decisions.unfilled([action])
        if gaps:
            raise ValueError(f"The reply still contains {gaps[0]}; it was not sent. Edit it and approve it again")
        if kind == "send_email":
            if any(channels.is_linkedin_address(t) for t in p["to"] + (p.get("cc") or [])):
                raise ValueError("Job Sourcer never emails LinkedIn addresses; LinkedIn messages are answered on "
                                 "LinkedIn")
            return await self.hub.call("mail", "send_email", {
                "to": p["to"], "subject": p.get("subject", ""), "body": p["body"], "cc": p.get("cc") or [],
                "in_reply_to": p.get("in_reply_to", ""), "references": p.get("references") or [],
            }, capability=self._cap)
        if kind == "send_linkedin":
            return await self.hub.call("linkedin", "send_message",
                                       {"conversation_id": p["conversation_id"], "text": p["text"], "force": force},
                                       capability=self._cap)
        if kind == "add_to_calendar":
            # A recruiter's invitation: put it on your calendar as is (no reply, no invites sent, never twice).
            return await self.hub.call("calendar", "add_invite", {
                "uid": p.get("uid", ""), "title": p["title"], "start": p["start"], "end": p["end"],
                "description": p.get("description", ""), "location": p.get("location", ""),
                "organizer_email": p.get("organizer_email", ""), "organizer_name": p.get("organizer_name", ""),
            }, capability=self._cap)
        if kind == "create_event":
            check = await self.hub.call("calendar", "check_availability", {
                "start": p["start"], "end": p["end"], "buffer_minutes": self.cfg.scheduling.buffer_minutes})
            if not check.get("free"):
                titles = ", ".join(c.get("title", "busy") for c in check.get("conflicts", []))
                raise SlotConflict(f"The slot is no longer free ({titles}); drafting again with fresh availability.")
            return await self.hub.call("calendar", "create_event", {
                "title": p["title"], "start": p["start"], "end": p["end"], "attendees": p.get("attendees", []),
                "description": p.get("description", ""), "location": p.get("location", ""),
                "add_video_link": bool(p.get("add_video_link", True)),
            }, capability=self._cap)
        if kind == "rsvp_event":
            raw = event.get("raw", {}) if isinstance(event.get("raw"), dict) else loads(event.get("raw_json"), {})
            if p.get("response", "accepted") == "accepted" and raw.get("start"):
                check = await self.hub.call("calendar", "check_availability",
                                            {"start": raw["start"], "end": raw["end"], "buffer_minutes": 0})
                others = [c for c in check.get("conflicts", []) if c.get("id") != p["event_id"]]
                if others:
                    raise SlotConflict("Something else was booked over this invitation; drafting again.")
            return await self.hub.call("calendar", "rsvp_event",
                                       {"event_id": p["event_id"], "response": p.get("response", "accepted")},
                                       capability=self._cap)
        raise ValueError(f"Unknown action type {kind}")

    def _after_success(self, action: dict[str, Any], result: dict[str, Any], draft: dict[str, Any],
                       event: dict[str, Any]) -> None:
        p = action["payload"]
        kind = action["type"]
        opp_id = draft["opportunity_id"]
        if kind in ("send_email", "send_linkedin"):
            body = p.get("body") if kind == "send_email" else p.get("text")
            ext = result.get("message_id", "")
            self.db.insert("outbound", {
                "draft_id": draft["id"], "channel": "email" if kind == "send_email" else "linkedin",
                "thread_key": event.get("thread_key"), "recipient": ", ".join(p.get("to", [])) or p.get("recipient", ""),
                "subject": p.get("subject", ""), "body": body, "external_id": ext, "sent_at": now_iso(),
            })
            if ext and event.get("thread_key"):
                self.db.execute("INSERT OR IGNORE INTO message_ids (message_id, thread_key) VALUES (?, ?)",
                                (ext, event["thread_key"]))
            pipeline.record_outbound(self.db, opp_id, "Awaiting recruiter reply")
        elif kind == "create_event":
            pipeline.record_outbound(self.db, opp_id, f"Call booked: {p['start'][:16].replace('T', ' ')} UTC")
            if opp_id:
                pipeline.set_stage(self.db, opp_id, "screening")
        elif kind == "rsvp_event" and p.get("response") == "accepted":
            pipeline.record_outbound(self.db, opp_id, "Interview accepted")
            if opp_id:
                pipeline.set_stage(self.db, opp_id, "interviewing")
