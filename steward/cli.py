"""`steward` command line. You brief it, it briefs you back; there is no chat."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from steward import briefing as briefing_mod
from steward import decisions
from steward.config import Config, load_config
from steward.db import Database, loads
from steward.ledger import Ledger
from steward.taxonomy import STAGES, TIER_LABELS

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="Job Sourcer: your recruiter-facing chief of staff.")
console = Console(highlight=False)
STATE: dict[str, Any] = {"config": None}


@app.callback()
def main(config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to steward.yaml"),
         verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    STATE["config"] = config
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(asctime)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def cfg() -> Config:
    try:
        return load_config(STATE["config"])
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def steward_obj():
    from steward.orchestrator import Steward

    return Steward(cfg())


def _matches(search: str, *fields: Any) -> bool:
    if not search:
        return True
    hay = " ".join(str(f or "") for f in fields).lower()
    return all(term in hay for term in search.lower().split())


def fail(msg: str) -> None:
    console.print(f"[red]{escape(msg)}[/red]")
    raise typer.Exit(1)


# -- setup ----------------------------------------------------------------------
@app.command()
def init(directory: Path = typer.Option(Path("workspace"), "--dir", help="Workspace folder to create"),
         sandbox: bool = typer.Option(False, "--sandbox", help="Seed a fake inbox, LinkedIn, and calendar"),
         force: bool = typer.Option(False, "--force", help="Overwrite an existing steward.yaml"),
         cv: Optional[Path] = typer.Option(None, "--cv", help="Your CV in markdown (copied in; name/email pre-filled)")) -> None:
    """Create a workspace: steward.yaml, .env, cv/, data/."""
    from steward.workspace import init_workspace

    try:
        path = init_workspace(directory, sandbox=sandbox, force=force, cv_source=cv)
    except FileExistsError as exc:
        fail(str(exc))
    console.print(f"[green]Workspace ready:[/green] {path}")
    if sandbox:
        c = load_config(path)
        console.print(f"Sandbox seeded (LLM provider: [bold]{c.llm.provider}[/bold]). Next:\n"
                      f"  steward -c \"{path}\" run --once\n  steward -c \"{path}\" brief\n"
                      f"  steward -c \"{path}\" serve")
    else:
        console.print("Next: put your CV in cv/cv.md, fill steward.yaml (profile, mail, calendar) and .env, "
                      "then run `steward doctor`.")


@app.command()
def doctor() -> None:
    """Check configuration, credentials, and that every MCP server starts."""
    from steward.cvparse import CV
    from steward.mcp_hub import MCPHub

    c = cfg()
    table = Table(title="Job Sourcer doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Result")

    def row(name: str, ok: bool | None, detail: str) -> None:
        mark = "[green]OK[/green]" if ok else ("[yellow]--[/yellow]" if ok is None else "[red]FAIL[/red]")
        table.add_row(name, f"{mark} {escape(detail)}")

    row("Config", True, c.config_path)
    row("Time zone", c.profile.timezone != "UTC" or None, c.profile.timezone)
    cv = CV.load(c.resolve(c.cv.path), c.cv.aliases)
    row("CV", bool(cv.lines), f"{len(cv.roles)} roles, {len(cv.list_skills())} skills, {cv.total_years()} years")
    import os

    if c.llm.provider == "claude-code":
        from steward.llm.claude_code_provider import auth_status

        st = auth_status(c.llm.claude_code_path)
        ok = st.get("loggedIn") and st.get("authMethod") == "claude.ai"
        row("Claude", bool(ok), f"subscription via Claude Code: {st.get('email') or 'not signed in'} "
                                f"({st.get('subscriptionType') or '?'} plan), model {c.llm.claude_code_model}"
            if st["installed"] else "Claude Code not installed (winget install Anthropic.ClaudeCode)")
    elif c.llm.provider == "anthropic":
        has = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        row("Claude", has or None, f"model {c.llm.model}" + ("" if has else " (no ANTHROPIC_API_KEY; an `ant auth login` profile also works)"))
    elif c.llm.provider == "grok":
        row("Grok", bool(os.environ.get(c.llm.grok_api_key_env)), c.llm.grok_model)
    else:
        row("LLM", None, "offline heuristics (demo only; set llm.provider: anthropic)")

    async def probe() -> None:
        async with MCPHub(c) as hub:
            for name in ("cv", "mail", "calendar", "linkedin", "notion"):
                if not hub.enabled(name):
                    row(f"MCP {name}", None, "disabled")
                    continue
                try:
                    tools = await hub.list_tools(name)
                    detail = f"{len(tools)} tools"
                    if name == "mail":
                        who = await hub.call("mail", "whoami", {})
                        detail += f", {who['address']} ({who['backend']})"
                    if name == "notion":
                        st = await hub.call("notion", "notion_status", {})
                        detail += f", database '{st['title']}'"
                    if name == "linkedin":
                        ps = await hub.call("linkedin", "pacing_status", {})
                        detail += f", {ps['sent_last_day']}/{ps['daily_cap']} sends today"
                    row(f"MCP {name}", True, detail)
                except Exception as exc:  # noqa: BLE001
                    row(f"MCP {name}", False, str(exc)[:160])

    asyncio.run(probe())
    console.print(table)


@app.command("google-auth")
def google_auth() -> None:
    """One-time OAuth for Google Calendar (uses secrets/google_credentials.json)."""
    c = cfg()
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        fail("Install the Google extra first: pip install -e .[google]")
    creds_file = c.resolve(c.calendar.credentials_file)
    if not creds_file.exists():
        fail(f"Download an OAuth client (Desktop app) JSON from Google Cloud Console to {creds_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), ["https://www.googleapis.com/auth/calendar"])
    creds = flow.run_local_server(port=0)
    token = c.resolve(c.calendar.token_file)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    console.print(f"[green]Saved Google token to {token}[/green]")


@app.command("notion-init")
def notion_init(parent_page: str = typer.Option(..., "--parent-page", help="ID of a Notion page shared with your integration")) -> None:
    """Create the pipeline database in Notion and print the id to put in steward.yaml."""
    from steward.mcp_hub import MCPHub

    c = cfg()
    c.notion.enabled = True

    async def go() -> dict[str, Any]:
        async with MCPHub(c) as hub:
            return await hub.call("notion", "create_database", {"parent_page_id": parent_page})

    res = asyncio.run(go())
    console.print(f"[green]Created.[/green] Set notion.enabled: true and notion.database_id: {res['database_id']}\n{res.get('url', '')}")


# -- the loop -------------------------------------------------------------------
@app.command()
def run(once: bool = typer.Option(False, "--once", help="Run a single cycle and exit"),
        tick: int = typer.Option(60, help="Seconds between cycles")) -> None:
    """Watch email, LinkedIn, and calendar; draft, route, auto-execute what is earned."""
    s = steward_obj()
    if once:
        summary = asyncio.run(s.cycle(force=True))
        _print_cycle(summary)
        return
    console.print(f"[bold]Job Sourcer is on duty.[/bold] Checking every {tick}s (Ctrl+C to stop).")
    from steward import linkedin_health

    li = asyncio.run(linkedin_health.check(s, "startup"))
    if li.get("problems"):
        for _, problem in li["problems"]:
            console.print(f"[yellow]! LinkedIn: {escape(problem)}[/yellow]")
    try:
        asyncio.run(s.run_forever(tick))
    except KeyboardInterrupt:
        console.print("Stopped.")


def _print_cycle(summary: dict[str, Any]) -> None:
    ing = summary.get("ingested", {})
    console.print(f"Ingested: {', '.join(f'{k} {v}' for k, v in ing.items()) or 'nothing'} | "
                  f"processed {summary.get('processed', 0)} | handled automatically {summary.get('auto_executed', 0)}")
    learning = summary.get("learning") or {}
    if any(learning.get(k) for k in ("rule_proposals", "promotion_proposals", "preference_proposals")):
        console.print("[magenta]New suggested automation is waiting in your briefing.[/magenta]")
    for err in summary.get("errors", []):
        console.print(f"[yellow]! {escape(str(err))}[/yellow]")


# -- briefing -------------------------------------------------------------------
def _briefing(with_calendar: bool = True) -> dict[str, Any]:
    from steward.mcp_hub import MCPHub
    from steward.notify import upcoming

    c = cfg()
    db = Database(c.db_path)
    meetings: list[dict[str, Any]] = []
    if with_calendar:
        async def get() -> list[dict[str, Any]]:
            async with MCPHub(c) as hub:
                return await upcoming(c, db, hub)

        try:
            meetings = asyncio.run(get())
        except Exception:  # noqa: BLE001
            meetings = []
    return briefing_mod.build(c, db, meetings)


def _badge(v: dict[str, Any]) -> str:
    parts = [f"[{'red' if v['band'] == 'LOW' else 'yellow' if v['band'] == 'MID' else 'green'}]{v['band']} {v['confidence']:.2f}[/]"]
    ja = v.get("job_advancement") or 0
    parts.append(f"[{'red' if ja < 0.5 else 'cyan'}]job-adv {ja:.2f}[/]")
    if v["status"] != "pending":
        parts.append(f"[bold magenta]{v['status'].upper()}[/]")
    return "  ".join(parts)


def _render_item(v: dict[str, Any], full: bool = False) -> Panel:
    ev = v["event"]
    head = f"[bold]#{v['id']}[/bold] [{v['channel']}] {escape(v['summary'] or '')}"
    lines = [f"From {escape(ev.get('sender_name') or '')} <{escape(ev.get('sender_address') or '')}>  {ev.get('received', '')}",
             _badge(v)]
    body = (ev.get("body") or "").strip()
    lines.append("[dim]" + escape(body if full else body[:400] + ("..." if len(body) > 400 else "")) + "[/dim]")
    for r in v["reasons"]:
        lines.append(f"  - {escape(r)}")
    for a in v["actions"]:
        p = a["payload"]
        skip = " [dim](skipped)[/dim]" if a.get("skip") else ""
        if a["type"] in ("send_email", "send_linkedin"):
            text = p.get("body") or p.get("text") or ""
            lines.append(f"[bold]Proposed reply ({a['type'].replace('send_', '')})[/bold]{skip}:")
            lines.append(escape(text if full else text[:700]))
        elif a["type"] == "create_event":
            lines.append(f"[bold]Book:[/bold] {escape(p['title'])} {p['start']} -> {p['end']}{skip}")
        elif a["type"] == "rsvp_event":
            lines.append(f"[bold]RSVP:[/bold] {p.get('response')} invitation {p['event_id']}{skip}")
    if not v["actions"]:
        lines.append("[dim]No action proposed (handle this one yourself, or reject to dismiss).[/dim]")
    border = "red" if v["priority"] == "high" else "blue"
    return Panel("\n".join(lines), title=head, title_align="left", border_style=border)


@app.command()
def brief(full: bool = typer.Option(False, "--full", help="Show full messages and drafts")) -> None:
    """Your briefing: high priority, decisions, what was handled, suggestions, meetings, pipeline."""
    b = _briefing()
    n = b["counts"]
    console.print(Panel(
        f"[bold]{n['waiting']}[/bold] waiting for you ({n['high_priority']} high priority, {n['staged']} already decided)  |  "
        f"[bold]{n['handled_automatically'] + n['handled_silently']}[/bold] handled automatically  |  "
        f"[bold]{n['suggestions']}[/bold] suggested automations  |  last 'go': {b['last_go_ago']}",
        title=f"Briefing for {b['generated_for']}", border_style="green"))
    if b["high_priority"]:
        console.rule("[red]High priority")
        for v in b["high_priority"]:
            console.print(_render_item(v, full))
    if b["needs_decision"]:
        console.rule("Needs your decision")
        for v in b["needs_decision"]:
            console.print(_render_item(v, full))
    if b["handled_automatically"] or b["handled_silently"]:
        console.rule("[green]Handled automatically")
        for v in b["handled_automatically"]:
            rule = v["rule"]["name"] if v["rule"] else ""
            console.print(f"  #{v['id']} {escape(v['summary'] or '')}  [dim](rule: {escape(rule)}; "
                          f"`steward override {v['id']} --note ...` if wrong)[/dim]")
        if b["handled_silently"]:
            console.print(f"  [dim]+ {len(b['handled_silently'])} handled silently (in the ledger).[/dim]")
    if b["in_flight"]:
        console.rule("In flight")
        for v in b["in_flight"]:
            console.print(f"  #{v['id']} {v['status']}: {escape(v['summary'] or '')} "
                          f"[dim]{escape(v.get('status_reason') or '')}[/dim]")
    if b["suggestions"]:
        console.rule("[magenta]Suggested automation")
        for p in b["suggestions"]:
            tag = "accept/dismiss" if p["status"] == "open" else "info"
            console.print(f"  [bold]P{p['id']}[/bold] {escape(p['title'])} [dim]({tag})[/dim]\n     {escape(p['rationale'] or '')}")
    if b["fyi"]:
        console.rule("FYI (no reply needed)")
        for f in b["fyi"]:
            console.print(f"  {f['source']}: {escape(f['summary'] or '')} [dim]{f['received']}[/dim]")
    if b["upcoming"]:
        console.rule("Upcoming meetings")
        for m in b["upcoming"]:
            console.print(f"  {m['when']}  [bold]{escape(m['title'])}[/bold]  {m.get('meeting_link') or ''}"
                          + ("  [yellow](not answered yet)[/yellow]" if m.get("my_response") == "needsAction" else ""))
    console.rule("Pipeline")
    console.print("  " + "  ".join(f"{STAGES[s]}: {c}" for s, c in b["pipeline"]["by_stage"].items() if c) or "  (empty)")
    console.print("\nDecide with [bold]steward review[/bold] (one sitting), or approve/edit/reject/dont-ask by id. "
                  "Then [bold]steward go[/bold].")


@app.command()
def show(draft_id: int) -> None:
    """Show one draft in full, with CV evidence and scheduling details."""
    c = cfg()
    db = Database(c.db_path)
    d = db.get_draft(draft_id)
    if not d:
        fail("No such draft")
    v = briefing_mod.draft_view(c, db, d)
    console.print(_render_item(v, full=True))
    if v["evidence"]:
        console.print("[bold]CV evidence[/bold]")
        for e in v["evidence"]:
            mark = "[green]found[/green]" if e.get("answer_found") else "[red]not in CV[/red]"
            yrs = f", {e['years']} yrs" if e.get("years") else ""
            console.print(f"  {mark} {escape(e.get('question', ''))} [dim]{escape(str(e.get('skill') or e.get('source')))}{yrs}[/dim]")
    if v["schedule"]["checks"] or v["schedule"]["slots"]:
        console.print(f"[bold]Schedule[/bold] ({v['schedule']['kind']})")
        for ch in v["schedule"]["checks"]:
            console.print(f"  {ch.get('label')}: {'free' if ch.get('ok', ch.get('free')) else ', '.join(ch.get('problems') or ['conflict'])}")
        for s in v["schedule"]["slots"]:
            console.print(f"  offered: {s}")


# -- decisions --------------------------------------------------------------------
def _db_ledger() -> tuple[Config, Database, Ledger]:
    c = cfg()
    db = Database(c.db_path)
    return c, db, Ledger(db)


@app.command()
def approve(draft_id: int, note: str = typer.Option("", help="Optional note for the ledger")) -> None:
    """Approve a draft as-is (executes at `steward go`)."""
    _, db, _ = _db_ledger()
    try:
        decisions.stage(db, draft_id, "approve", note=note)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[green]#{draft_id} approved.[/green]")


@app.command()
def reject(draft_id: int, note: str = typer.Option("", help="Why (teaches Job Sourcer)")) -> None:
    """Reject a draft: nothing is sent."""
    _, db, _ = _db_ledger()
    try:
        decisions.stage(db, draft_id, "reject", note=note)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[yellow]#{draft_id} rejected.[/yellow]")


@app.command()
def edit(draft_id: int, note: str = typer.Option("", help="Optional note")) -> None:
    """Edit the reply (and meeting time) in your editor, then approve it."""
    _, db, _ = _db_ledger()
    d = db.get_draft(draft_id)
    if not d:
        fail("No such draft")
    actions = loads(d["final_actions_json"], None) or loads(d["actions_json"], [])
    editable = [{"type": a["type"], "skip": a.get("skip", False),
                 "payload": {k: v for k, v in a["payload"].items() if k in decisions.EDITABLE_FIELDS.get(a["type"], set())}}
                for a in actions]
    text = typer.edit(json.dumps(editable, indent=2, ensure_ascii=False), extension=".json")
    if text is None:
        console.print("No changes.")
        return
    try:
        decisions.stage(db, draft_id, "approve", json.loads(text), note=note)
    except (ValueError, decisions.DecisionError) as exc:
        fail(str(exc))
    console.print(f"[green]#{draft_id} edited and approved.[/green]")


@app.command()
def reset(draft_id: int) -> None:
    """Undo a staged decision (before `go`)."""
    _, db, _ = _db_ledger()
    try:
        decisions.reset(db, draft_id)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"#{draft_id} is pending again.")


@app.command("dont-ask")
def dont_ask(draft_id: int,
             silent: bool = typer.Option(False, "--silent", help="Act silently (auto_send) instead of act-and-tell-me"),
             tech: list[str] = typer.Option([], "--tech", help="Limit the rule to these technologies (repeatable)"),
             sender: str = typer.Option("", help="Limit to a sender address or @domain"),
             company: str = typer.Option("", help="Limit to a company"),
             channel: list[str] = typer.Option([], "--channel", help="Limit to email/linkedin/calendar"),
             min_confidence: Optional[float] = typer.Option(None, help="Still ask me below this confidence")) -> None:
    """"Don't ask me again" for this kind of decision: creates a rule and approves this draft."""
    c, db, ledger = _db_ledger()
    try:
        res = decisions.dont_ask_again(c, db, ledger, draft_id, tier="auto_send" if silent else "auto_approve",
                                       technologies=tech, channels=channel, sender=sender, company=company,
                                       min_confidence=min_confidence)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[green]Rule #{res['rule_id']} '{escape(res['name'])}' active ({TIER_LABELS[res['tier']]}).[/green]\n"
                  f"  Applies to: {escape(res['description'])}")


@app.command()
def override(draft_id: int, note: str = typer.Option(..., help="What was wrong"),
             corrected: str = typer.Option("", help="What you would have sent instead")) -> None:
    """Flag something Job Sourcer did on its own as wrong. Its rule is demoted immediately."""
    c, db, ledger = _db_ledger()
    try:
        res = decisions.override(c, db, ledger, draft_id, note, corrected)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[yellow]Recorded. Rule demoted to {res['demoted_to']}.[/yellow]" if res["demoted_to"]
                  else "[yellow]Recorded.[/yellow]")


@app.command()
def review() -> None:
    """Go through every waiting decision in one sitting, then say go."""
    c, db, ledger = _db_ledger()
    b = briefing_mod.build(c, db)
    items = [v for v in b["high_priority"] + b["needs_decision"] if v["status"] == "pending"]
    if not items and not b["suggestions"]:
        console.print("Nothing waiting for you.")
    for v in items:
        console.print(_render_item(v, full=True))
        while True:
            choice = typer.prompt("[a]pprove  [e]dit  [r]eject  [d]on't ask again  [s]kip", default="s").strip().lower()
            try:
                if choice == "a":
                    decisions.stage(db, v["id"], "approve")
                elif choice == "r":
                    decisions.stage(db, v["id"], "reject", note=typer.prompt("Why? (optional)", default=""))
                elif choice == "e":
                    edit(v["id"], note="")
                elif choice == "d":
                    if not v["dont_ask"]["allowed"]:
                        console.print(f"[red]{v['dont_ask']['blocked_reason']}[/red]")
                        continue
                    silent = typer.confirm("Act silently (no briefing entry)? No = act and tell me", default=False)
                    res = decisions.dont_ask_again(c, db, ledger, v["id"], tier="auto_send" if silent else "auto_approve")
                    console.print(f"[green]Rule '{escape(res['name'])}' created.[/green]")
                elif choice != "s":
                    continue
            except decisions.DecisionError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                continue
            break
    for p in [p for p in b["suggestions"] if p["status"] == "open"]:
        console.print(Panel(escape(p["rationale"] or ""), title=f"P{p['id']} {escape(p['title'])}", border_style="magenta"))
        if typer.confirm("Accept?", default=False):
            decisions.decide_proposal(c, db, ledger, p["id"], True)
        elif typer.confirm("Dismiss it?", default=False):
            decisions.decide_proposal(c, db, ledger, p["id"], False)
    staged = db.scalar("SELECT COUNT(*) FROM drafts WHERE status IN ('approved', 'edited', 'rejected')")
    if staged and typer.confirm(f"{staged} decision(s) made. Go (execute them all now)?", default=True):
        go(yes=True)


@app.command()
def go(yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation")) -> None:
    """Commit your decisions to the ledger and execute everything approved, in one batch."""
    c, db, _ = _db_ledger()
    staged = db.query("SELECT id, status, summary FROM drafts WHERE status IN ('approved', 'edited', 'rejected')")
    if not staged and not db.scalar("SELECT COUNT(*) FROM drafts WHERE status IN ('queued', 'deferred')"):
        console.print("No decisions to execute.")
        return
    if not yes:
        for s in staged:
            console.print(f"  #{s['id']} {s['status']}: {escape(s['summary'] or '')}")
        if not typer.confirm(f"Execute {sum(1 for s in staged if s['status'] != 'rejected')} approved action(s)?", default=True):
            return
    result = asyncio.run(steward_obj().go())
    console.print(f"[green]Done.[/green] executed {result['executed']}, deferred {result['deferred']} "
                  f"(human-paced LinkedIn), failed {result['failed']}, re-drafted {result.get('superseded', 0)}.")
    for det in result["details"]:
        if det["status"] != "executed":
            console.print(f"  #{det['draft_id']} {det['status']}: {escape(det.get('reason') or '')}")
    for err in result.get("errors", []):
        console.print(f"[yellow]! {escape(str(err))}[/yellow]")


# -- learning ---------------------------------------------------------------------
@app.command()
def accept(proposal_id: int) -> None:
    """Accept a suggested automation (P<id> in the briefing)."""
    c, db, ledger = _db_ledger()
    try:
        decisions.decide_proposal(c, db, ledger, proposal_id, True)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[green]P{proposal_id} accepted.[/green]")


@app.command()
def dismiss(proposal_id: int) -> None:
    """Dismiss a suggested automation."""
    c, db, ledger = _db_ledger()
    try:
        decisions.decide_proposal(c, db, ledger, proposal_id, False)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"P{proposal_id} dismissed.")


@app.command()
def rules(action: str = typer.Argument("list", help="list | retire | reactivate | promote | demote"),
          rule_id: Optional[int] = typer.Argument(None),
          status: str = typer.Option("", help="Only rules with this status (active, shadow, dormant, ...)"),
          search: str = typer.Option("", help="Only rules whose name or scope contains this text"),
          sort: str = typer.Option("id", help="Sort by: id, name, matched, executed, overrides")) -> None:
    """Show the autonomy ladder, or change a rule."""
    from steward.rules import rule_view

    c, db, ledger = _db_ledger()
    if action != "list":
        if rule_id is None:
            fail("Give a rule id")
        try:
            decisions.rule_action(db, ledger, rule_id, action)
        except decisions.DecisionError as exc:
            fail(str(exc))
        console.print(f"Rule #{rule_id}: {action} done.")
        return
    t = Table(title="Decision rules (the autonomy ladder)")
    for col in ("#", "Name", "Tier", "Status", "Applies to", "Matched", "Executed", "Overrides"):
        t.add_column(col)
    views = [rule_view(r) for r in db.query("SELECT * FROM rules ORDER BY id")]
    views = [v for v in views if (not status or v["status"] == status)
             and _matches(search, v["name"], v["description"])]
    key = {"name": lambda v: v["name"].lower(), "matched": lambda v: -v["times_matched"],
           "executed": lambda v: -v["times_executed"], "overrides": lambda v: -v["overrides"]}.get(sort, lambda v: v["id"])
    for v in sorted(views, key=key):
        status = v["status"] + (f" ({v['shadow_agreements']}/{c.autonomy.shadow_matches_required})" if v["status"] == "shadow" else "")
        t.add_row(str(v["id"]), escape(v["name"]), v["tier_label"], status, escape(v["description"] or ""),
                  str(v["times_matched"]), str(v["times_executed"]), str(v["overrides"]))
    console.print(t)


@app.command()
def prefs(add: str = typer.Option("", "--add", help="Add a standing instruction for every draft"),
          retire: Optional[int] = typer.Option(None, "--retire", help="Retire a preference by id")) -> None:
    """Your standing preferences (constraints every draft follows)."""
    _, db, ledger = _db_ledger()
    if add:
        pid = decisions.add_preference(db, ledger, add)
        console.print(f"[green]Preference #{pid} active.[/green]")
    if retire is not None:
        decisions.retire_preference(db, ledger, retire)
        console.print(f"Preference #{retire} retired.")
    for p in db.query("SELECT * FROM preferences WHERE status != 'retired' ORDER BY id"):
        console.print(f"  #{p['id']} [{p['status']}] {escape(p['text'])} [dim]({p['origin']})[/dim]")


@app.command()
def tasks(add: str = typer.Option("", "--add", help="Add a task with this title"),
          due: str = typer.Option("", help="Due date/time for --add, e.g. 2026-10-30T17:00"),
          link: str = typer.Option("", help="Link for --add"),
          done: Optional[int] = typer.Option(None, "--done", help="Mark a task done"),
          reopen: Optional[int] = typer.Option(None, "--reopen", help="Reopen a task"),
          accept: Optional[int] = typer.Option(None, "--accept", help="Accept a task Job Sourcer suggested"),
          dismiss: Optional[int] = typer.Option(None, "--dismiss", help="Dismiss a task"),
          search: str = typer.Option("", help="Only tasks containing this text")) -> None:
    """Your to-do list: assessments, forms, documents, applications."""
    from steward import tasks as tasks_mod

    c, db, led = _db_ledger()
    try:
        if add:
            tasks_mod.add(c, db, led, add, due_at=due or None, link=link)
        for task_id, action in ((done, "done"), (reopen, "reopen"), (accept, "accept"), (dismiss, "dismiss")):
            if task_id is not None:
                tasks_mod.act(c, db, led, task_id, action)
    except tasks_mod.TaskError as exc:
        fail(str(exc))
    t = Table(title="Tasks")
    for col in ("#", "Status", "Task", "Kind", "Opportunity", "Due", "Link"):
        t.add_column(col)
    for row in tasks_mod.listing(c, db):
        if not _matches(search, row["title"], row["details"], row["company"]):
            continue
        due_txt = ("OVERDUE " if row["overdue"] else "") + row["due_label"]
        t.add_row(str(row["id"]), row["status"], escape(row["title"]), row["kind_label"], escape(row["company"]),
                  due_txt, escape(row["link"] or ""))
    console.print(t)


@app.command()
def instruct(draft_id: int, instruction: str = typer.Argument(..., help="What Job Sourcer should do instead")) -> None:
    """Tell Job Sourcer what to do instead of its draft; it rewrites the reply and actions for your approval."""
    s = steward_obj()
    try:
        res = asyncio.run(s.revise(draft_id, instruction=instruction))
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[green]Revised as #{res['draft_id']}:[/green] {escape(res['summary'])}")
    for w in res["warnings"]:
        console.print(f"[yellow]! {escape(w)}[/yellow]")
    show(res["draft_id"])


@app.command()
def teach(draft_id: int, rule: str = typer.Argument(..., help="How Job Sourcer should respond next time"),
          scope: str = typer.Option("kind", help="kind (messages like this), company, sender, or all"),
          redraft: bool = typer.Option(False, "--redraft", help="Also re-draft this item with the rule")) -> None:
    """Save a rule from a briefing item that shapes future replies."""
    c, db, led = _db_ledger()
    try:
        res = decisions.teach_rule(db, led, draft_id, rule, scope)
    except decisions.DecisionError as exc:
        fail(str(exc))
    console.print(f"[green]Rule saved[/green] for {escape(res['applies_to'])}.")
    if redraft:
        out = asyncio.run(steward_obj().revise(draft_id, reason=f"Re-drafted with your new rule: “{rule}”"))
        console.print(f"Re-drafted as #{out['draft_id']}.")


@app.command()
def pipeline(csv_out: Optional[Path] = typer.Option(None, "--csv", help="Export to CSV"),
             set_stage: Optional[str] = typer.Option(None, "--set-stage", help="Stage to set (needs --id)"),
             opp_id: Optional[int] = typer.Option(None, "--id"),
             search: str = typer.Option("", help="Only rows containing this text"),
             stage: str = typer.Option("", help="Only this stage"),
             sort: str = typer.Option("updated", help="Sort by: updated, company, stage, recruiter")) -> None:
    """Every opportunity in your pipeline (searchable, filterable, sortable)."""
    from steward.pipeline import set_stage as _set

    c, db, _ = _db_ledger()
    if set_stage:
        if opp_id is None or set_stage not in STAGES:
            fail(f"Use --id N --set-stage one of: {', '.join(STAGES)}")
        _set(db, opp_id, set_stage, force=True)
        console.print(f"Opportunity #{opp_id} -> {STAGES[set_stage]}")
    rows = db.query("SELECT * FROM opportunities ORDER BY updated_at DESC")
    rows = [r for r in rows if (not stage or r["stage"] == stage)
            and _matches(search, r["company"], r["role"], r["recruiter_name"], r["recruiter_email"], r["next_action"])]
    from steward.taxonomy import STAGE_ORDER

    key = {"company": lambda r: (r["company"] or "").lower(), "recruiter": lambda r: (r["recruiter_name"] or "").lower(),
           "stage": lambda r: -STAGE_ORDER.get(r["stage"], 0)}.get(sort)
    if key:
        rows.sort(key=key)
    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id"])
            w.writeheader()
            w.writerows(rows)
        console.print(f"Wrote {len(rows)} rows to {csv_out}")
        return
    t = Table(title="Pipeline")
    for col in ("#", "Company", "Role", "Stage", "Recruiter", "Channel", "Next action"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r["id"]), escape(r["company"] or ""), escape(r["role"] or ""), STAGES.get(r["stage"], r["stage"]),
                  escape(r["recruiter_name"] or ""), r["channel"] or "", escape(r["next_action"] or ""))
    console.print(t)


@app.command()
def ledger(verify: bool = typer.Option(False, "--verify", help="Recompute the hash chain"),
           tail: int = typer.Option(15, help="Show the last N entries"),
           kind: str = typer.Option("", help="Only this kind (decision, execution, auto_execution, override, ...)"),
           search: str = typer.Option("", help="Only entries whose content contains this text")) -> None:
    """The immutable decision ledger: every decision, action, and rule change."""
    _, db, led = _db_ledger()
    if verify:
        res = led.verify()
        if res["ok"]:
            console.print(f"[green]Ledger intact:[/green] {res['entries']} entries, head {res.get('head', '')[:16]}...")
        else:
            fail(f"Ledger broken at seq {res['broken_at']}: {res['reason']}")
        return
    entries = [e for e in led.entries(kind or None, limit=100000) if _matches(search, json.dumps(e["payload"]))]
    for e in reversed(entries[:tail]):
        p = e["payload"]
        what = p.get("decision") or p.get("change") or p.get("status") or p.get("kind") or ""
        console.print(f"  {e['seq']:>4} {e['ts']} [bold]{e['kind']}[/bold] {what} "
                      f"[dim]draft={e['draft_id'] or '-'} rule={e['rule_id'] or '-'}[/dim]")


# -- dashboard ----------------------------------------------------------------------
@app.command()
def serve(watch: bool = typer.Option(True, "--watch/--no-watch", help="Also run the background loop"),
          port: Optional[int] = typer.Option(None),
          tick: Optional[int] = typer.Option(None, help="Seconds between checks (default: dashboard.tick_seconds)")) -> None:
    """Run the dashboard and keep watching in the background (foreground process)."""
    import uvicorn

    from steward.web.app import create_app

    c = cfg()
    application = create_app(c, watch=watch, tick=tick)
    p = port or c.dashboard.port
    console.print(f"[bold]Job Sourcer[/bold] dashboard: http://{c.dashboard.host}:{p}")
    uvicorn.run(application, host=c.dashboard.host, port=p, log_level="warning")


def _running(url: str, config_path: str = "") -> str:
    """'' if nothing answers, 'same' if this workspace's Job Sourcer answers, 'other' for another workspace."""
    import httpx

    try:
        data = httpx.get(f"{url}/healthz", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return ""
    if data.get("app") != "steward":
        return "other"
    same = not config_path or Path(data.get("config", "")).resolve() == Path(config_path).resolve()
    return "same" if same else "other"


def _open_window(url: str) -> None:
    """Open the dashboard as an app window (Edge/Chrome app mode), else in the default browser."""
    import os
    import shutil
    import subprocess
    import webbrowser

    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if base:
            candidates += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                           os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for exe in candidates:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}"], close_fds=True)
            return
    webbrowser.open(url)


@app.command("app")
def app_window(no_window: bool = typer.Option(False, "--no-window", help="Only make sure Job Sourcer is running")) -> None:
    """Start Job Sourcer in the background (if needed) and open the dashboard window. No terminal to keep open."""
    import subprocess
    import time

    c = cfg()
    url = f"http://{c.dashboard.host}:{c.dashboard.port}"
    state = _running(url, c.config_path)
    if state == "other":
        fail(f"Port {c.dashboard.port} is used by something else (maybe another Job Sourcer workspace). "
             "Close it, or set dashboard.port in steward.yaml.")
    if not state:
        log_dir = c.data_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        flags, python = 0, sys.executable
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            # pythonw: no console at all. A console Python started without one gets a new, visible (empty)
            # console window, which Claude Code then renames "claude". Output still goes to server.log.
            windowless = Path(sys.executable).with_name("pythonw.exe")
            python = str(windowless) if windowless.exists() else python
        with open(log_dir / "server.log", "a", encoding="utf-8") as out:
            subprocess.Popen([python, "-m", "steward", "-c", c.config_path, "serve"], stdout=out, stderr=out,
                             stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        for _ in range(60):
            if _running(url, c.config_path) == "same":
                break
            time.sleep(0.5)
        else:
            fail(f"Job Sourcer did not start; see {log_dir / 'server.log'}")
    if not no_window:
        _open_window(url)
    console.print(f"Job Sourcer is running at {url}")


@app.command()
def autostart(enable: bool = typer.Option(True, "--enable/--disable")) -> None:
    """Start Job Sourcer automatically when you log in to Windows."""
    from steward.setup import set_autostart

    try:
        on = set_autostart(cfg(), enable)
    except RuntimeError as exc:
        fail(str(exc))
    console.print("Job Sourcer will start when you log in." if on else "Start-at-login removed.")


# -- sandbox helpers ------------------------------------------------------------------
@app.command("sandbox-email")
def sandbox_email(from_name: str = typer.Option(...), from_email: str = typer.Option(...),
                  subject: str = typer.Option(...), body: str = typer.Option(...)) -> None:
    """Drop a new recruiter email into the sandbox inbox."""
    from steward.workspace import inject_email

    uid = inject_email(cfg(), from_name, from_email, subject, body)
    console.print(f"Injected sandbox email UID {uid}.")


@app.command("sandbox-linkedin")
def sandbox_linkedin(name: str = typer.Option(...), text: str = typer.Option(...),
                     headline: str = typer.Option("Recruiter"), conversation: str = typer.Option("")) -> None:
    """Drop a new LinkedIn message into the sandbox."""
    from steward.workspace import inject_linkedin

    cid = inject_linkedin(cfg(), name, headline, text, conversation)
    console.print(f"Injected sandbox LinkedIn message in {cid}.")


if __name__ == "__main__":
    app()
