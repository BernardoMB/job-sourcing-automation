"""Notion events -> your calendar, by scraping Notion in a browser signed in to your own account.

No Notion integration, API token, or public API is used. Playwright drives an installed browser
(Microsoft Edge by default) with a dedicated profile in `workspace/secrets/notion-browser/`:

- If that profile is signed in to Notion, everything runs headless: no window appears.
- If it is not (the first time, or after Notion ends the session), a normal browser window opens on
  the Notion login page. You sign in; Job Sourcer notices, keeps the session in the profile, closes the
  window by itself, and carries on headless.

Inside the signed-in browser Job Sourcer reads what the Notion web app itself loads (see notion_scrape):
every workspace in your account, every database with a date property, and its dated rows. Each
item from today through `notion.lookahead_days` is put on your calendar through the calendar MCP
server (`sync_external_event`), tagged with the Notion page id so it is never added twice.

Every run is written to `data/logs/notion-sync.log`: a START line, one line per workspace, an ADDED
line for every event put on your calendar (UPDATED for changed ones), and exactly one result line:
SYNC OK, SYNC FAILED, or NEEDS LOGIN.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from steward.config import Config
from steward.db import Database
from steward.mcp_hub import MCPHub, MCPToolError
from steward.timeutil import now_iso, parse_iso, to_iso, tz, utcnow

log = logging.getLogger("steward.notion")

# The signed-in Notion web app. (www.notion.so now redirects signed-in and signed-out users to notion.com.)
NOTION = "https://app.notion.com"
LOG_NAME = "notion-sync.log"
STATE_KEY = "notion:synced"
LAST_KEY = "notion:last_sync"

# One browser profile can be open by one browser at a time.
_profile_lock = threading.Lock()


class NeedsLogin(RuntimeError):
    pass


class ScrapeError(RuntimeError):
    pass


# -- the log ----------------------------------------------------------------------
class SyncLog:
    """Plain, append-only, human-readable log of every sync run."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, tag: str, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {tag:<9} {message}".rstrip()
        with self._lock, open(self.path, "a", encoding="utf-8") as out:
            out.write(line + "\n")
        log.info("notion %s %s", tag, message)

    def tail(self, lines: int = 300) -> str:
        if not self.path.exists():
            return ""
        with open(self.path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])


def log_path(cfg: Config) -> Path:
    return cfg.data_path / "logs" / LOG_NAME


def profile_path(cfg: Config) -> Path:
    return cfg.resolve(cfg.notion.profile_dir)


def forget_session(cfg: Config) -> bool:
    """Delete the browser profile (signs Job Sourcer out of Notion). The next sync asks you to sign in."""
    path = profile_path(cfg)
    with _profile_lock:
        if not path.exists():
            return False
        shutil.rmtree(path)
    SyncLog(log_path(cfg)).write("LOGIN", "session forgotten: browser profile deleted; the next sync opens the login window")
    return True


# -- items ------------------------------------------------------------------------
@dataclass
class NotionItem:
    id: str
    title: str
    start: str  # YYYY-MM-DD or YYYY-MM-DDTHH:MM
    end: str = ""
    all_day: bool = True
    time_zone: str = ""
    workspace: str = ""
    database: str = ""
    url: str = ""

    def window(self, default_tz: str) -> tuple[str, str]:
        """ISO start/end for the calendar: dates for all-day items, aware datetimes otherwise."""
        if self.all_day:
            return self.start[:10], (self.end or self.start)[:10]
        zone = tz(self.time_zone or default_tz)
        s = datetime.fromisoformat(self.start).replace(tzinfo=zone)
        e = datetime.fromisoformat(self.end).replace(tzinfo=zone) if len(self.end) > 10 else s + timedelta(hours=1)
        return to_iso(s), to_iso(e if e > s else s + timedelta(hours=1))

    def first_day(self) -> date:
        return date.fromisoformat(self.start[:10])

    def last_day(self) -> date:
        return date.fromisoformat((self.end or self.start)[:10])

    def label(self, default_tz: str) -> str:
        if self.all_day:
            return self.start[:10] + (f" to {self.end[:10]}" if self.end and self.end[:10] != self.start[:10] else "")
        s, e = self.window(default_tz)
        sd, ed = parse_iso(s), parse_iso(e)
        zone = tz(self.time_zone or default_tz)
        sd, ed = sd.astimezone(zone), ed.astimezone(zone)
        same_day = sd.date() == ed.date()
        text = f"{sd:%Y-%m-%d %H:%M}-{ed:%H:%M}" if same_day else f"{sd:%Y-%m-%d %H:%M} to {ed:%Y-%m-%d %H:%M}"
        return text + (f" ({self.time_zone})" if self.time_zone and self.time_zone != default_tz else "")


@dataclass
class ScrapeResult:
    items: list[NotionItem] = field(default_factory=list)
    workspaces: list[dict[str, Any]] = field(default_factory=list)  # {"id", "name", "items", "error"}
    logged_in_now: bool = False


# -- the browser ------------------------------------------------------------------
class NotionBrowser:
    """A Playwright browser session on a persistent profile. Use from a worker thread (sync API)."""

    def __init__(self, cfg: Config, sync_log: SyncLog):
        self.cfg = cfg
        self.log = sync_log
        self.profile = profile_path(cfg)

    def _launch(self, pw: Any, headless: bool) -> Any:
        n = self.cfg.notion
        self.profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
            "viewport": {"width": 1280, "height": 900} if headless else None,
        }
        if n.browser != "chromium":
            kwargs["channel"] = n.browser
        try:
            return pw.chromium.launch_persistent_context(str(self.profile), **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise ScrapeError(f"could not start {n.browser} through Playwright: {str(exc).splitlines()[0]}") from exc

    @staticmethod
    def _has_cookie(ctx: Any) -> bool:
        return any(c["name"] == "token_v2" and "notion" in c.get("domain", "") for c in ctx.cookies())

    @staticmethod
    def _page(ctx: Any) -> Any:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return page

    def _open_notion(self, page: Any) -> None:
        page.goto(f"{NOTION}/", wait_until="domcontentloaded", timeout=60_000)

    def api(self, page: Any, endpoint: str, body: dict[str, Any], user_id: str = "", space_id: str = "") -> Any:
        """POST to the Notion web app's own endpoints from inside the signed-in page."""
        headers = {"content-type": "application/json"}
        if user_id:
            headers["x-notion-active-user-header"] = user_id
        if space_id:
            headers["x-notion-space-id"] = space_id
        res = page.evaluate(
            """async ([url, body, headers]) => {
                const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body), credentials: 'include'});
                return {status: r.status, text: await r.text()};
            }""", [f"/api/v3/{endpoint}", body, headers])
        if res["status"] == 401:
            raise NeedLoginSignal()
        if res["status"] >= 400:
            raise ScrapeError(f"Notion answered {res['status']} to {endpoint}: {res['text'][:200]}")
        try:
            return json.loads(res["text"] or "{}")
        except ValueError as exc:
            raise ScrapeError(f"Notion sent something unexpected for {endpoint}: {res['text'][:120]!r}") from exc

    def _signed_in(self, ctx: Any, page: Any = None) -> bool:
        """Does Notion accept this profile's session? Asked with the profile's cookies, whatever page is open."""
        if not self._has_cookie(ctx):
            return False
        try:
            res = ctx.request.post(f"{NOTION}/api/v3/getSpaces", data={},
                                   headers={"content-type": "application/json"}, timeout=30_000)
            return res.ok
        except Exception:  # noqa: BLE001 - network hiccup or the browser closing; ask again later
            return False

    def _login(self, pw: Any) -> None:
        minutes = self.cfg.notion.login_timeout_minutes
        self.log.write("LOGIN", f"not signed in to Notion: opening a browser window for you to sign in "
                                f"(waits {minutes} min)")
        ctx = self._launch(pw, headless=False)
        try:
            page = self._page(ctx)
            page.goto(f"{NOTION}/login", wait_until="domcontentloaded", timeout=60_000)
            try:
                page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            deadline = time.monotonic() + minutes * 60
            while time.monotonic() < deadline:
                if not ctx.pages:
                    raise NeedsLogin("the sign-in window was closed before signing in")
                if self._signed_in(ctx):
                    time.sleep(3)  # let Notion finish writing its cookies and local storage
                    self.log.write("LOGIN", "signed in; session saved in the browser profile; window closed")
                    return
                time.sleep(2)
            raise NeedsLogin(f"nobody signed in within {minutes} minutes")
        finally:
            ctx.close()

    def scrape(self, interactive: bool, lookahead_days: int, only_calendar_views: bool) -> ScrapeResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScrapeError('Playwright is not installed: run  .venv\\Scripts\\pip install -e ".[notion]"') from exc
        result = ScrapeResult()
        with _profile_lock, sync_playwright() as pw:
            ctx = self._launch(pw, headless=True)
            try:
                page = self._page(ctx)
                self._open_notion(page)
                ok = self._signed_in(ctx, page)
            except Exception:
                ctx.close()
                raise
            if not ok:
                ctx.close()
                if not interactive:
                    raise NeedsLogin("the saved Notion session is signed out")
                self._login(pw)
                result.logged_in_now = True
                ctx = self._launch(pw, headless=True)
                page = self._page(ctx)
                self._open_notion(page)
                if not self._signed_in(ctx, page):
                    ctx.close()
                    raise ScrapeError("signed in, but the hidden browser does not see the session")
            try:
                from steward import notion_scrape

                notion_scrape.collect(self, page, result, lookahead_days, only_calendar_views,
                                      self.cfg.profile.timezone)
            finally:
                ctx.close()
        return result


class NeedLoginSignal(Exception):
    """Notion answered 401: the session in the profile is signed out."""


# -- the sync -----------------------------------------------------------------------
def describe(item: NotionItem) -> str:
    lines = [f"From Notion ({item.workspace}{' / ' + item.database if item.database else ''})"]
    if item.url:
        lines.append(item.url)
    lines.append("Added by Job Sourcer's Notion sync. Edit the item in Notion; changes are copied here.")
    return "\n".join(lines)


async def sync(cfg: Config, db: Database, hub: MCPHub, *, interactive: bool = True, reason: str = "scheduled",
               scraper: Callable[[], ScrapeResult] | None = None) -> dict[str, Any]:
    """One full run: scrape Notion in the browser, then add/update the events on your calendar."""
    import asyncio

    slog = SyncLog(log_path(cfg))
    started = time.monotonic()
    profile_exists = profile_path(cfg).exists()
    slog.write("START", f"notion sync ({reason}; headless; saved session: {'yes' if profile_exists else 'none yet'})")
    summary: dict[str, Any] = {"status": "", "at": now_iso(), "added": 0, "updated": 0, "unchanged": 0,
                               "found": 0, "workspaces": 0, "errors": [], "added_titles": []}

    def finish(status: str, detail: str) -> dict[str, Any]:
        summary["status"] = status
        summary["detail"] = detail
        summary["seconds"] = round(time.monotonic() - started, 1)
        slog.write({"ok": "SYNC OK", "failed": "SYNC FAILED", "needs_login": "NEEDS LOGIN"}[status],
                   f"{detail} ({summary['seconds']} s)")
        db.kv_set(LAST_KEY, summary)
        return summary

    if not hub.enabled("calendar"):
        return finish("failed", "no calendar configured (Setup step 5); nothing to sync into")
    browser = NotionBrowser(cfg, slog)
    try:
        result = await asyncio.to_thread(scraper or (lambda: browser.scrape(
            interactive, cfg.notion.lookahead_days, cfg.notion.only_calendar_views)))
    except NeedsLogin as exc:
        return finish("needs_login", f"not signed in to Notion: {exc}. The next run asks again.")
    except Exception as exc:  # noqa: BLE001 - every failure is logged, never raised into the loop
        log.exception("notion scrape failed")
        return finish("failed", f"scraping Notion failed: {type(exc).__name__}: {exc}")

    for ws in result.workspaces:
        if ws.get("error"):
            slog.write("WORKSPACE", f"{ws['name']}: could not be read: {ws['error']}")
            summary["errors"].append(f"{ws['name']}: {ws['error']}")
        else:
            pages = f"{ws['pages']} page(s), " if ws.get("pages") else ""
            slog.write("WORKSPACE", f"{ws['name']}: {pages}{ws.get('databases', 0)} database(s) with dates, "
                                    f"{ws['items']} dated item(s) in range")
        for note in ws.get("notes", []):
            slog.write("NOTE", f"{ws['name']}: {note}")
    summary["workspaces"] = len(result.workspaces)
    summary["found"] = len(result.items)

    known: dict[str, Any] = db.kv_get(STATE_KEY, {}) or {}
    seen: set[str] = set()
    for item in result.items:
        seen.add(item.id)
        start, end = item.window(cfg.profile.timezone)
        try:
            res = await hub.call("calendar", "sync_external_event", {
                "source_key": f"notion:{item.id}", "title": item.title, "start": start, "end": end,
                "all_day": item.all_day, "description": describe(item)}, timeout=60)
        except MCPToolError as exc:
            summary["errors"].append(f"{item.title}: {exc}")
            slog.write("ERROR", f'"{item.title}" {item.label(cfg.profile.timezone)} [{item.workspace}] '
                                f"could not be put on the calendar: {exc}")
            continue
        status = res.get("status")
        where = f"[{item.workspace}{' / ' + item.database if item.database else ''}]"
        if status == "added":
            summary["added"] += 1
            summary["added_titles"].append(item.title)
            slog.write("ADDED", f'"{item.title}" {item.label(cfg.profile.timezone)} {where} {item.url}')
        elif status == "updated":
            summary["updated"] += 1
            slog.write("UPDATED", f'"{item.title}" now {item.label(cfg.profile.timezone)} {where} '
                                  f"(changed: {', '.join(res.get('changed', []))})")
        elif status == "deleted_by_you":
            summary["unchanged"] += 1
            if known.get(item.id, {}).get("status") != "deleted_by_you":
                slog.write("SKIPPED", f'"{item.title}" {where}: you deleted it from your calendar; not re-adding')
        else:
            summary["unchanged"] += 1
        known[item.id] = {"title": item.title, "start": item.start, "workspace": item.workspace,
                          "event_id": res.get("id", ""), "status": status if status == "deleted_by_you" else "synced"}

    # Items that disappeared from Notion stay on the calendar; say so once.
    read_ok = {ws["name"] for ws in result.workspaces if not ws.get("error")}
    today = date.today().isoformat()
    for item_id, info in list(known.items()):
        if item_id in seen:
            continue
        if info.get("start", "")[:10] < today:
            known.pop(item_id)  # in the past: forget it
            continue
        if info.get("workspace") in read_ok:
            slog.write("GONE", f'"{info.get("title")}" ({info.get("start")}) is no longer in Notion or no longer dated '
                               "in range; its calendar event was left as is")
            known.pop(item_id)
    db.kv_set(STATE_KEY, known)

    counts = (f"{summary['workspaces']} workspace(s), {summary['found']} event(s) found, {summary['added']} added, "
              f"{summary['updated']} updated, {summary['unchanged']} unchanged")
    if summary["errors"] and not (summary["added"] or summary["updated"] or summary["unchanged"]):
        return finish("failed", f"{counts}; {len(summary['errors'])} error(s): {summary['errors'][0]}")
    if summary["errors"]:
        return finish("ok", f"{counts}; {len(summary['errors'])} problem(s), see the lines above")
    return finish("ok", counts)


def due(cfg: Config, db: Database) -> bool:
    last = parse_iso((db.kv_get(LAST_KEY) or {}).get("at"))
    return last is None or (utcnow() - last).total_seconds() >= cfg.notion.poll_minutes * 60


def last_status(cfg: Config, db: Database) -> dict[str, Any]:
    return db.kv_get(LAST_KEY) or {}
