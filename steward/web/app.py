"""Local dashboard: the whole day-to-day workflow without a terminal.

- Watches continuously in the background and pushes every change to the page
  (Server-Sent Events), so new events show up, re-prioritized, within seconds.
- Decisions are buttons; "Go" runs them in one batch, or each approval runs
  immediately if you switch that on.
- Setup (credentials, profile, Google, Notion, start-at-login) is a page too.

Bound to 127.0.0.1 only. Every state-changing request must carry a per-process
token that is embedded in the page, so other websites open in your browser
cannot trigger anything, and the Host header is checked to stop DNS-rebinding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from steward import ASSETS
from steward import briefing as briefing_mod
from steward import decisions
from steward import linkedin_bridge
from steward import setup as setup_mod
from steward import sound
from steward import tasks as tasks_mod
from steward.llm import LLMError
from steward.config import Config
from steward.db import loads
from steward.llm.schemas import CRITIQUE_SCHEMA
from steward.mcp_hub import MCPHub
from steward.notify import upcoming
from steward.orchestrator import Steward
from steward.pipeline import set_stage
from steward.taxonomy import STAGES, TIER_LABELS
from steward.timeutil import fmt_local, now_iso

log = logging.getLogger("steward.web")
STATIC = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def create_app(cfg: Config, watch: bool = True, tick: int | None = None) -> FastAPI:
    steward = Steward(cfg)
    db, ledger = steward.db, steward.ledger
    token = secrets.token_urlsafe(24)
    lock = asyncio.Lock()
    meetings_cache: dict[str, Any] = {"at": 0.0, "data": []}
    state: dict[str, Any] = {"running": False, "last_error": "", "version": 0, "last_event": {}}

    def C() -> Config:
        return steward.cfg

    def bump(kind: str, **info: Any) -> None:
        state["version"] += 1
        state["last_event"] = {"kind": kind, "at": now_iso(), **info}

    def execute_on_approve() -> bool:
        return bool(db.kv_get("settings", {}).get("execute_on_approve", C().autonomy.execute_on_approve))

    async def run_cycle(force: bool = False) -> dict[str, Any]:
        async with lock:
            state["running"] = True
            bump("checking")
            try:
                summary = await steward.cycle(force=force)
                state["last_error"] = ""
            except Exception as exc:  # noqa: BLE001
                state["last_error"] = str(exc)
                log.exception("cycle failed")
                summary = {"errors": [str(exc)], "processed": 0, "auto_executed": 0, "ingested": {}}
            finally:
                state["running"] = False
        meetings_cache["at"] = 0.0
        new = sum(v for v in summary.get("ingested", {}).values() if isinstance(v, int))
        rep = summary.get("reprioritized") or {}
        bump("cycle", new=new, processed=summary.get("processed", 0), auto=summary.get("auto_executed", 0),
             redrafted=rep.get("redrafted", 0), escalated=rep.get("escalated", 0),
             waiting=db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'pending'") or 0,
             high=db.scalar("SELECT COUNT(*) FROM drafts WHERE status = 'pending' AND priority = 'high'") or 0,
             errors=summary.get("errors", []))
        return summary

    async def background() -> None:
        await asyncio.sleep(1)
        while True:
            await run_cycle()
            await asyncio.sleep(max(int(tick or C().dashboard.tick_seconds), 5))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        steward.rescore()
        task = asyncio.create_task(background()) if watch else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Job Sourcer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        sent = request.headers.get("x-steward-token") or request.query_params.get("token")
        if request.url.path == "/api/stream" and sent != token:
            return JSONResponse({"error": "missing or invalid token"}, status_code=403)
        if request.method != "GET" and sent != token:
            return JSONResponse({"error": "missing or invalid token"}, status_code=403)
        return await call_next(request)

    async def meetings() -> list[dict[str, Any]]:
        if time.time() - meetings_cache["at"] > 120:
            try:
                async with MCPHub(C()) as hub:
                    meetings_cache["data"] = await upcoming(C(), db, hub)
            except Exception:  # noqa: BLE001
                meetings_cache["data"] = []
            meetings_cache["at"] = time.time()
        return meetings_cache["data"]

    def bad(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    async def maybe_run_now(draft_id: int) -> dict[str, Any] | None:
        if not execute_on_approve():
            return None
        async with lock:
            result = await steward.go_one(draft_id)
        meetings_cache["at"] = 0.0
        return result

    # -- pages and live updates -------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"app": "steward", "ok": True, "config": C().config_path}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return html.replace("__STEWARD_TOKEN__", token)

    # The page's icon, which is also what Edge/Chrome show in the taskbar for the app window.
    @app.get("/app-logo.png")
    @app.get("/favicon.ico")
    async def logo() -> FileResponse:
        return FileResponse(ASSETS / "app-logo.png", media_type="image/png")

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def events():
            seen = -1
            last_beat = time.time()
            while not await request.is_disconnected():
                if state["version"] != seen:
                    seen = state["version"]
                    payload = {"version": seen, "running": state["running"], **state["last_event"]}
                    yield f"data: {json.dumps(payload, default=str)}\n\n"
                elif time.time() - last_beat > 15:
                    last_beat = time.time()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/briefing")
    async def get_briefing() -> dict[str, Any]:
        b = briefing_mod.build(C(), db, await meetings())
        b["loop"] = {"watching": watch, "running": state["running"], "last_error": state["last_error"],
                     "tick": int(tick or C().dashboard.tick_seconds),
                     "polls": {"email": C().mail.poll_minutes, "calendar": C().calendar.poll_minutes,
                               "linkedin": C().linkedin.poll_minutes}}
        b["stages"] = STAGES
        b["settings"] = {"execute_on_approve": execute_on_approve()}
        return b

    @app.get("/api/history")
    async def history(limit: int = 1000) -> list[dict[str, Any]]:
        rows = db.query("SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (min(limit, 5000),))
        out = []
        for d in rows:
            v = briefing_mod.draft_view(C(), db, d)
            decided = {"pending": "waiting for you", "approved": "approved (not run yet)",
                       "edited": "edited (not run yet)", "rejected": "rejected (not committed yet)",
                       "queued": "approved, queued", "executing": "running", "executed": "done",
                       "deferred": "waiting (LinkedIn pacing)", "failed": "failed", "dismissed": "rejected",
                       "superseded": "replaced by a newer draft"}.get(d["status"], d["status"])
            who = "Job Sourcer (rule)" if d["route"] != "ask" and d["status"] in ("executed", "queued", "executing") else \
                ("you" if d["decided_at"] else "")
            out.append({
                "id": d["id"], "summary": d["summary"], "channel": d["channel"], "status": d["status"],
                "outcome": decided, "decided_by": who, "route": TIER_LABELS.get(d["route"], d["route"]),
                "priority": d["priority"], "confidence": d["confidence"], "job_advancement": d["job_advancement"],
                "company": (v["opportunity"] or {}).get("company", ""), "categories": v["categories"],
                "created": d["created_at"], "created_label": fmt_local(d["created_at"], C().profile.timezone),
                "executed_label": fmt_local(d["executed_at"], C().profile.timezone) if d["executed_at"] else "",
                "reason": d.get("status_reason") or "", "note": d.get("decision_note") or "",
                "message": (v["event"].get("body") or "")[:3000], "from": v["event"].get("sender_name") or "",
                "actions": v["actions"],
            })
        return out

    # -- decisions --------------------------------------------------------------
    @app.post("/api/drafts/{draft_id}/decision")
    async def decide(draft_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            d = decisions.stage(db, draft_id, body.get("decision", ""), body.get("actions"), body.get("note", ""))
        except decisions.DecisionError as exc:
            raise bad(exc)
        ran = await maybe_run_now(draft_id)
        bump("decision", id=draft_id)
        return {"id": draft_id, "status": d["status"], "executed": ran}

    @app.post("/api/drafts/{draft_id}/reset")
    async def reset(draft_id: int) -> dict[str, Any]:
        try:
            decisions.reset(db, draft_id)
        except decisions.DecisionError as exc:
            raise bad(exc)
        bump("decision", id=draft_id)
        return {"id": draft_id, "status": "pending"}

    @app.post("/api/drafts/{draft_id}/dont-ask")
    async def dont_ask(draft_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if body.get("actions"):
                decisions.stage(db, draft_id, "approve", body["actions"], body.get("note", ""))
            res = decisions.dont_ask_again(
                C(), db, ledger, draft_id, tier=body.get("tier", "auto_approve"),
                categories=body.get("categories") or None, technologies=body.get("technologies") or [],
                channels=body.get("channels") or [], sender=body.get("sender", ""), company=body.get("company", ""),
                min_confidence=body.get("min_confidence"))
        except decisions.DecisionError as exc:
            raise bad(exc)
        res["executed"] = await maybe_run_now(draft_id)
        bump("decision", id=draft_id)
        return res

    @app.post("/api/drafts/{draft_id}/override")
    async def override(draft_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            res = decisions.override(C(), db, ledger, draft_id, body.get("note", ""), body.get("corrected", ""))
        except decisions.DecisionError as exc:
            raise bad(exc)
        bump("decision", id=draft_id)
        return res

    @app.post("/api/drafts/{draft_id}/instruct")
    async def instruct(draft_id: int, body: dict[str, Any]) -> dict[str, Any]:
        text = (body.get("instruction") or "").strip()
        if not text:
            raise HTTPException(400, "Write what Job Sourcer should do instead")
        try:
            async with lock:
                res = await steward.revise(draft_id, instruction=text)
        except (decisions.DecisionError, tasks_mod.TaskError) as exc:
            raise bad(exc)
        except LLMError as exc:
            raise HTTPException(503, f"Claude could not process the instruction right now: {exc}")
        bump("revised", id=res["draft_id"])
        return res

    @app.post("/api/drafts/{draft_id}/rule")
    async def item_rule(draft_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            res = decisions.teach_rule(db, ledger, draft_id, body.get("text", ""), body.get("scope", "kind"))
            if body.get("redraft"):
                async with lock:
                    res["revised"] = await steward.revise(
                        draft_id, reason=f"Re-drafted with your new rule: “{body.get('text', '').strip()}”")
        except decisions.DecisionError as exc:
            raise bad(exc)
        except LLMError as exc:
            raise HTTPException(503, f"Rule saved, but Claude could not re-draft right now: {exc}")
        bump("rule", id=draft_id)
        return res

    # -- tasks ---------------------------------------------------------------------
    @app.get("/api/tasks")
    async def list_tasks(dismissed: bool = False) -> dict[str, Any]:
        return {"tasks": tasks_mod.listing(C(), db, include_dismissed=dismissed), "kinds": tasks_mod.KINDS,
                "opportunities": [{"id": o["id"], "label": " - ".join(x for x in (o["company"], o["role"]) if x)
                                   or f"Opportunity {o['id']}"}
                                  for o in db.query("SELECT id, company, role FROM opportunities ORDER BY company")]}

    @app.post("/api/tasks")
    async def create_task(body: dict[str, Any]) -> dict[str, Any]:
        try:
            task_id = tasks_mod.add(C(), db, ledger, body.get("title", ""), details=body.get("details", ""),
                                    kind=body.get("kind", "other"), link=body.get("link", ""),
                                    due_at=body.get("due_at") or None,
                                    opportunity_id=int(body["opportunity_id"]) if body.get("opportunity_id") else None)
        except tasks_mod.TaskError as exc:
            raise bad(exc)
        steward.rescore()
        bump("task", id=task_id)
        return {"id": task_id}

    @app.post("/api/tasks/{task_id}")
    async def change_task(task_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            task = tasks_mod.act(C(), db, ledger, task_id, body.get("action", ""), body.get("fields"))
        except tasks_mod.TaskError as exc:
            raise bad(exc)
        steward.rescore()
        bump("task", id=task_id)
        return task

    @app.post("/api/proposals/{proposal_id}")
    async def proposal(proposal_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            res = decisions.decide_proposal(C(), db, ledger, proposal_id, bool(body.get("accept")))
        except decisions.DecisionError as exc:
            raise bad(exc)
        bump("proposal", id=proposal_id)
        return res

    @app.post("/api/rules/{rule_id}")
    async def rule(rule_id: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            decisions.rule_action(db, ledger, rule_id, body.get("action", ""))
        except decisions.DecisionError as exc:
            raise bad(exc)
        bump("rule", id=rule_id)
        return {"ok": True}

    @app.get("/api/preferences")
    async def preferences() -> list[dict[str, Any]]:
        return db.query("SELECT * FROM preferences WHERE status != 'retired' ORDER BY id")

    @app.post("/api/preferences")
    async def add_pref(body: dict[str, Any]) -> dict[str, Any]:
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty preference")
        return {"id": decisions.add_preference(db, ledger, text, body.get("categories") or [])}

    @app.post("/api/preferences/{pref_id}/retire")
    async def retire_pref(pref_id: int) -> dict[str, Any]:
        try:
            decisions.retire_preference(db, ledger, pref_id)
        except decisions.DecisionError as exc:
            raise bad(exc)
        return {"ok": True}

    @app.post("/api/pipeline/{opp_id}")
    async def pipeline_stage(opp_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("stage") not in STAGES:
            raise HTTPException(400, "unknown stage")
        set_stage(db, opp_id, body["stage"], force=True)
        steward.rescore()
        bump("pipeline", id=opp_id)
        return {"ok": True}

    @app.post("/api/poll")
    async def poll() -> dict[str, Any]:
        summary = await run_cycle(force=True)
        return {k: v for k, v in summary.items() if k != "learning"}

    @app.post("/api/go")
    async def go() -> dict[str, Any]:
        async with lock:
            result = await steward.go()
        meetings_cache["at"] = 0.0
        result.pop("learning", None)
        bump("go", executed=result.get("executed", 0))
        return result

    @app.post("/api/settings")
    async def settings(body: dict[str, Any]) -> dict[str, Any]:
        current = db.kv_get("settings", {}) or {}
        if "execute_on_approve" in body:
            current["execute_on_approve"] = bool(body["execute_on_approve"])
        db.kv_set("settings", current)
        bump("settings")
        return current

    @app.get("/api/ledger")
    async def ledger_entries(limit: int = 500) -> list[dict[str, Any]]:
        return ledger.entries(limit=min(limit, 5000))

    @app.get("/api/ledger/verify")
    async def ledger_verify() -> dict[str, Any]:
        return ledger.verify()

    @app.get("/api/notifications")
    async def notifications() -> list[dict[str, Any]]:
        rows = db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 30")
        for r in rows:
            r["channels"] = loads(r.pop("channels_json"), [])
        return rows

    # -- setup ------------------------------------------------------------------
    @app.get("/api/setup")
    async def setup_status() -> dict[str, Any]:
        cfg_now = C()
        sf = setup_mod.startup_file()
        checks = await asyncio.to_thread(setup_mod.checks, cfg_now)
        return {
            "checks": checks,
            "secrets": setup_mod.secret_status(cfg_now),
            "secret_labels": setup_mod.SECRETS,
            "values": setup_mod.editable_values(cfg_now),
            "paths": {"config": cfg_now.config_path, "env": str(setup_mod.env_file(cfg_now)),
                      "cv": str(cfg_now.resolve(cfg_now.cv.path)),
                      "google_credentials": str(cfg_now.resolve(cfg_now.calendar.credentials_file))},
            "autostart": bool(sf and sf.exists()),
            "autostart_supported": sf is not None,
        }

    def reload(new_cfg: Config) -> None:
        steward.cfg = new_cfg
        steward._llm = None
        meetings_cache["at"] = 0.0

    @app.post("/api/setup/config")
    async def setup_config(body: dict[str, Any]) -> dict[str, Any]:
        try:
            reload(setup_mod.update_config(C(), body.get("changes") or {}))
        except (ValueError, TypeError) as exc:
            raise bad(exc)
        bump("settings")
        return {"ok": True}

    @app.post("/api/setup/secret")
    async def setup_secret(body: dict[str, Any]) -> dict[str, Any]:
        try:
            setup_mod.set_secret(C(), body.get("name", ""), body.get("value", ""))
        except ValueError as exc:
            raise bad(exc)
        reload(C())
        return {"ok": True}

    @app.post("/api/setup/sound-test")
    async def setup_sound_test() -> dict[str, Any]:
        path = sound.chime_file(C())
        if await asyncio.to_thread(sound.play, path, 3.0):
            return {"ok": True, "detail": f"Playing {path.name}"}
        return {"ok": False, "detail": f"Could not play {path} (the file is missing, or this is not Windows)"}

    @app.post("/api/setup/probe/{name}")
    async def setup_probe(name: str) -> dict[str, Any]:
        tools = {"mail": ("whoami", {}), "calendar": ("pending_invites", {"days": 2}), "cv": ("get_profile", {}),
                 "linkedin": ("upstream_tools", {"refresh": True}), "notion": ("notion_status", {})}
        if name == "llm":
            try:
                out = await asyncio.to_thread(steward.llm.json, "critique", "Reply with the JSON object requested.",
                                              "Score this reply: 'Thanks, Tuesday 3pm works.'",
                                              CRITIQUE_SCHEMA, {
                                                  "draft": {"confidence": 0.9}, "classification": {}})
                return {"ok": True, "detail": f"{steward.llm.name} answered (verdict: {out.get('verdict')})"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "detail": str(exc)}
        if name not in tools:
            raise HTTPException(404, "unknown integration")
        tool, args = tools[name]
        if name == "linkedin":
            await asyncio.to_thread(linkedin_bridge.ensure_running, C())
        try:
            async with MCPHub(C()) as hub:
                res = await hub.call(name, tool, args, timeout=120)
                if name == "linkedin" and res.get("tools"):
                    # Reaching the bridge is not enough: the browser extension must be connected too.
                    await hub.call("linkedin", "list_conversations", {"since": now_iso(), "limit": 1}, timeout=120)
                    res["extension"] = "connected"
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)[:500]}
        detail = {
            "mail": lambda r: f"Connected to {r.get('address')} ({r.get('backend')}, folder {r.get('folder')})",
            "calendar": lambda r: f"Connected. {len(r.get('invites', []))} unanswered invitation(s) in the next 2 days.",
            "cv": lambda r: f"{r.get('name')}: {r.get('total_years')} years, {len(r.get('recent_roles', []))} recent roles",
            "linkedin": lambda r: (f"Bridge and browser extension connected. Reading with "
                                   f"{r['mapping'].get('list_conversations')} / {r['mapping'].get('get_conversation')}, "
                                   f"replying with {r['mapping'].get('send_message')}.") if r.get("tools")
            else f"backend {r.get('backend')}",
            "notion": lambda r: f"Database '{r.get('title')}' reachable",
        }[name](res)
        return {"ok": True, "detail": detail}

    @app.post("/api/setup/google-auth")
    async def setup_google() -> dict[str, Any]:
        try:
            path = await asyncio.to_thread(setup_mod.google_connect, C())
        except Exception as exc:  # noqa: BLE001
            raise bad(exc)
        meetings_cache["at"] = 0.0
        return {"ok": True, "token": path}

    @app.post("/api/setup/notion")
    async def setup_notion(body: dict[str, Any]) -> dict[str, Any]:
        try:
            page = setup_mod.notion_page_id(body.get("page", ""))
            temp = C().model_copy(deep=True)
            temp.notion.enabled = True
            async with MCPHub(temp) as hub:
                res = await hub.call("notion", "create_database", {"parent_page_id": page})
            reload(setup_mod.update_config(C(), {"notion.enabled": True, "notion.database_id": res["database_id"]}))
        except Exception as exc:  # noqa: BLE001
            raise bad(exc)
        return {"ok": True, **res}

    @app.post("/api/setup/autostart")
    async def setup_autostart(body: dict[str, Any]) -> dict[str, Any]:
        try:
            on = setup_mod.set_autostart(C(), bool(body.get("enable")))
        except RuntimeError as exc:
            raise bad(exc)
        return {"ok": True, "enabled": on}

    return app
