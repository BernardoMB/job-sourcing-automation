"""Notion events -> your calendar, by scraping Notion in a browser signed in to your own account.

No Notion integration, API token, or public API is used. Job Sourcer starts the Playwright MCP server
(`@playwright/mcp`, through npx) and drives it as an MCP client: the server runs your installed browser
(Microsoft Edge by default) on a dedicated profile in `workspace/secrets/notion-browser/`:

- If that profile is signed in to Notion, the server runs the browser headless: no window appears.
- If it is not (the first time, or after Notion ends the session), a second server starts with a normal
  browser window on the Notion login page. You sign in; Job Sourcer notices, the session stays in the
  profile, the window closes by itself, and the run carries on headless.

Inside the signed-in browser (browser_evaluate) Job Sourcer reads what the Notion web app itself loads
(see notion_scrape): every workspace in your account, every database with a date property, and its
dated rows. Each item from today through `notion.lookahead_days` is put on your calendar through the
calendar MCP server (`sync_external_event`), tagged with the Notion page id so it is never added twice.
A new Notion item is therefore picked up by the next run, every `notion.poll_minutes`.

Every run is written to `data/logs/notion-sync.log`: a START line, BROWSER lines (Playwright MCP
started, headless, signed in), one line per workspace, an ADDED line for every new event put on your
calendar (UPDATED for changed ones), and exactly one result line: SYNC OK, SYNC FAILED, or NEEDS LOGIN.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from contextlib import AsyncExitStack
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
# Seconds to wait for the Playwright MCP server to start (the first start downloads it through npx).
STARTUP_SECONDS = 180
# Seconds for one Playwright MCP tool call (one Notion request).
CALL_SECONDS = 90

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


def mcp_dir(cfg: Config) -> Path:
    """The Playwright MCP server's working folder: its config files and any output it writes."""
    return cfg.data_path / "notion-mcp"


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


# -- the browser: the Playwright MCP server ----------------------------------------
def _result_text(res: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in (res.content or []) if getattr(c, "text", None))


def parse_result(text: str) -> Any:
    """The value a browser_evaluate call returned: the JSON in its "### Result" section."""
    marker = "### Result\n"
    at = text.find(marker)
    if at < 0:
        raise ScrapeError(f"the Playwright MCP server answered without a result: {text[:200]!r}")
    body = text[at + len(marker):]
    end = body.find("\n### ")  # the next section; JSON never holds a raw newline inside a string
    body = (body if end < 0 else body[:end]).strip()
    if body in ("", "undefined"):
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ScrapeError(f"the Playwright MCP server returned something unexpected: {body[:200]!r}") from exc


class PlaywrightMCP:
    """One Playwright MCP server (stdio) driving the installed browser on Job Sourcer's Notion profile.

    Headless for every normal run; a visible window only for signing in. Leaving the context closes the
    browser (so the profile is free for the next run) and stops the server.
    """

    def __init__(self, cfg: Config, headless: bool):
        self.cfg = cfg
        self.headless = headless
        self.session: Any = None
        self._stack: AsyncExitStack | None = None

    def _config_file(self) -> Path:
        """The server's --config: which browser, which profile, headless or not."""
        n = self.cfg.notion
        launch: dict[str, Any] = {
            "headless": self.headless,
            # Notion (and Google's sign-in) refuse browsers that announce automation.
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignoreDefaultArgs": ["--enable-automation"],
        }
        if n.browser != "chromium":
            launch["channel"] = n.browser
        browser: dict[str, Any] = {"browserName": "chromium", "userDataDir": str(profile_path(self.cfg)),
                                   "launchOptions": launch}
        if self.headless:
            browser["contextOptions"] = {"viewport": {"width": 1280, "height": 900}}
        path = mcp_dir(self.cfg) / f"playwright-{'headless' if self.headless else 'login'}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"browser": browser}, indent=2), encoding="utf-8")
        return path

    def _params(self) -> Any:
        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment

        spec = self.cfg.notion.playwright_mcp
        command = shutil.which(spec.command) or spec.command
        if not Path(command).exists():
            raise ScrapeError(f"could not start the Playwright MCP server: '{spec.command}' was not found "
                              "(install Node.js from nodejs.org, then restart Job Sourcer)")
        args = [*spec.args, "--config", str(self._config_file()), "--snapshot-mode", "none",
                "--image-responses", "omit", "--output-dir", str(mcp_dir(self.cfg))]
        return StdioServerParameters(command=command, args=args, cwd=str(mcp_dir(self.cfg)),
                                     env={**get_default_environment(), **spec.env})

    def describe(self) -> str:
        spec = self.cfg.notion.playwright_mcp
        package = next((a for a in spec.args if "playwright" in a), spec.command)
        return f"{package}, {self.cfg.notion.browser}, {'headless' if self.headless else 'visible window'}"

    async def __aenter__(self) -> "PlaywrightMCP":
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        mcp_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        profile_path(self.cfg).mkdir(parents=True, exist_ok=True)
        params = self._params()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            errlog = stack.enter_context(open(log_path(self.cfg).parent / "mcp-playwright.log", "a", encoding="utf-8"))
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            self.session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self.session.initialize(), timeout=STARTUP_SECONDS)
        except BaseException as exc:
            self.session = None
            await stack.aclose()
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                raise ScrapeError(f"the Playwright MCP server did not start within {STARTUP_SECONDS} s") from exc
            if isinstance(exc, Exception) and not isinstance(exc, ScrapeError):
                raise ScrapeError(f"could not start the Playwright MCP server: {type(exc).__name__}: {exc}") from exc
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.close_browser()
        finally:
            self.session = None
            if self._stack is not None:
                stack, self._stack = self._stack, None
                await stack.aclose()

    async def call(self, tool: str, args: dict[str, Any], timeout: float = CALL_SECONDS) -> str:
        res = await self.session.call_tool(tool, args, read_timeout_seconds=timeout)
        text = _result_text(res)
        if res.is_error:
            raise ScrapeError(f"Playwright MCP {tool} failed: {text[:300]}")
        return text

    async def navigate(self, url: str) -> None:
        await self.call("browser_navigate", {"url": url}, timeout=90)

    async def evaluate(self, function: str) -> Any:
        return parse_result(await self.call("browser_evaluate", {"function": function}))

    async def close_browser(self) -> None:
        if self.session is None:
            return
        try:
            await self.call("browser_close", {}, timeout=30)
        except Exception:  # noqa: BLE001 - the browser may already be gone
            pass

    # -- Notion -------------------------------------------------------------------
    async def api(self, page: Any, endpoint: str, body: dict[str, Any], user_id: str = "", space_id: str = "") -> Any:
        """POST to the Notion web app's own endpoints from inside the signed-in page (browser_evaluate)."""
        headers = {"content-type": "application/json"}
        if user_id:
            headers["x-notion-active-user-header"] = user_id
        if space_id:
            headers["x-notion-space-id"] = space_id
        call = json.dumps([f"{NOTION}/api/v3/{endpoint}", json.dumps(body), headers])
        res: Any = None
        for attempt in range(3):
            try:
                res = await self.evaluate(_FETCH_JS.replace("__CALL__", call))
                break
            except ScrapeError as exc:  # the page reloaded under us, or Notion was slow: ask again
                if attempt == 2:
                    raise
                log.info("notion %s retry: %s", endpoint, exc)
                await asyncio.sleep(2 * (attempt + 1))
        if not isinstance(res, dict):
            raise ScrapeError(f"Notion sent nothing usable for {endpoint}")
        if res.get("status") == 401:
            raise NeedLoginSignal()
        if res.get("status", 0) >= 400:
            raise ScrapeError(f"Notion answered {res['status']} to {endpoint}: {res.get('text', '')[:200]}")
        if "data" not in res:
            raise ScrapeError(f"Notion sent something unexpected for {endpoint}: {res.get('text', '')[:120]!r}")
        return res["data"]

    async def signed_in(self) -> tuple[bool, str]:
        """(does Notion accept this profile's session, the URL the window is on). Never raises."""
        try:
            res = await self.evaluate(_SIGNED_IN_JS)
        except Exception as exc:  # noqa: BLE001 - mid-redirect while signing in; ask again shortly
            return False, f"(busy: {str(exc)[:80]})"
        if not isinstance(res, dict):
            return False, ""
        return res.get("status") == 200, str(res.get("href", ""))


# Runs inside the page: one Notion endpoint, answered as {status, data} (or {status, text} on failure).
_FETCH_JS = """async () => {
  const [url, body, headers] = __CALL__;
  const r = await fetch(url, {method: 'POST', headers, body, credentials: 'include'});
  const text = await r.text();
  if (!r.ok) return {status: r.status, text: text.slice(0, 300)};
  try { return {status: r.status, data: JSON.parse(text || '{}')}; }
  catch (e) { return {status: r.status, text: text.slice(0, 300)}; }
}"""

# Runs inside the page: is this browser signed in to Notion? From a page that is not Notion's (Google's
# sign-in, say) the browser refuses the request, which just means "not yet".
_SIGNED_IN_JS = """async () => {
  const href = location.href;
  try {
    const r = await fetch('https://app.notion.com/api/v3/getSpaces', {method: 'POST',
      headers: {'content-type': 'application/json'}, body: '{}', credentials: 'include'});
    return {status: r.status, href};
  } catch (e) { return {status: 0, href}; }
}"""


class NotionBrowser:
    """Reads Notion through the Playwright MCP server: headless, plus a sign-in window only when needed."""

    def __init__(self, cfg: Config, sync_log: SyncLog):
        self.cfg = cfg
        self.log = sync_log

    async def _open_headless(self) -> tuple[PlaywrightMCP, bool]:
        mcp = PlaywrightMCP(self.cfg, headless=True)
        await mcp.__aenter__()
        try:
            self.log.write("BROWSER", f"Playwright MCP server started ({mcp.describe()}); no window is shown")
            # A plain page on Notion's own site: the endpoints are same-origin from here, and nothing on it
            # navigates by itself (the full Notion app would, sometimes mid-request).
            await mcp.navigate(f"{NOTION}/robots.txt")
            ok, _ = await mcp.signed_in()
        except BaseException:
            await mcp.__aexit__(None, None, None)
            raise
        return mcp, ok

    async def _login(self) -> None:
        minutes = self.cfg.notion.login_timeout_minutes
        self.log.write("LOGIN", "not signed in to Notion: opening a browser window (Playwright MCP) for you to "
                                f"sign in (waits {minutes} min)")
        async with PlaywrightMCP(self.cfg, headless=False) as mcp:
            await mcp.navigate(f"{NOTION}/login")
            deadline = time.monotonic() + minutes * 60
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                ok, href = await mcp.signed_in()
                if ok:
                    await asyncio.sleep(3)  # let Notion finish writing its cookies
                    self.log.write("LOGIN", "signed in; session saved in the browser profile; window closed")
                    return
                if href == "about:blank":
                    # You closed the window; the server opened an empty one to answer. That one closes on exit.
                    raise NeedsLogin("the sign-in window was closed before signing in")
            raise NeedsLogin(f"nobody signed in within {minutes} minutes")

    async def scrape(self, interactive: bool, lookahead_days: int, only_calendar_views: bool) -> ScrapeResult:
        from steward import notion_scrape

        result = ScrapeResult()
        async with _ProfileLock():
            mcp, ok = await self._open_headless()
            try:
                if not ok:
                    await mcp.__aexit__(None, None, None)
                    if not interactive:
                        raise NeedsLogin("the saved Notion session is signed out")
                    await self._login()
                    result.logged_in_now = True
                    mcp, ok = await self._open_headless()
                    if not ok:
                        raise ScrapeError("signed in, but the headless browser does not see the session")
                self.log.write("BROWSER", "signed in to Notion (saved session); reading your workspaces in the background")
                await notion_scrape.collect(mcp, None, result, lookahead_days, only_calendar_views,
                                            self.cfg.profile.timezone)
            finally:
                await mcp.__aexit__(None, None, None)
        return result


class _ProfileLock:
    """One browser at a time on the profile: a dashboard sync and a scheduled one never overlap."""

    async def __aenter__(self) -> None:
        while not _profile_lock.acquire(blocking=False):
            await asyncio.sleep(1)

    async def __aexit__(self, *exc: Any) -> None:
        _profile_lock.release()


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
    """One full run: scrape Notion through the Playwright MCP server, then add/update the events on your calendar."""
    slog = SyncLog(log_path(cfg))
    started = time.monotonic()
    profile_exists = profile_path(cfg).exists()
    slog.write("START", f"notion sync ({reason}) through the Playwright MCP server; saved session: "
                        f"{'yes' if profile_exists else 'none yet'}")
    summary: dict[str, Any] = {"status": "", "at": now_iso(), "added": 0, "updated": 0, "unchanged": 0,
                               "found": 0, "workspaces": 0, "errors": [], "added_titles": []}

    def finish(status: str, detail: str) -> dict[str, Any]:
        summary["status"] = status
        summary["detail"] = detail
        summary["seconds"] = round(time.monotonic() - started, 1)
        prefix = {"ok": "Notion sync through Playwright MCP succeeded: ",
                  "failed": "Notion sync through Playwright MCP failed: ", "needs_login": ""}[status]
        slog.write({"ok": "SYNC OK", "failed": "SYNC FAILED", "needs_login": "NEEDS LOGIN"}[status],
                   f"{prefix}{detail} ({summary['seconds']} s)")
        db.kv_set(LAST_KEY, summary)
        return summary

    if not hub.enabled("calendar"):
        return finish("failed", "no calendar configured (Setup step 5); nothing to sync into")
    browser = NotionBrowser(cfg, slog)
    try:
        if scraper is not None:
            result = await asyncio.to_thread(scraper)
        else:
            result = await browser.scrape(interactive, cfg.notion.lookahead_days, cfg.notion.only_calendar_views)
    except NeedsLogin as exc:
        return finish("needs_login", f"not signed in to Notion: {exc}. The next run asks again.")
    except Exception as exc:  # noqa: BLE001 - every failure is logged, never raised into the loop
        log.exception("notion scrape failed")
        return finish("failed", f"could not read Notion: {type(exc).__name__}: {exc}")

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
            slog.write("ADDED", f'new event on your calendar: "{item.title}" {item.label(cfg.profile.timezone)} '
                                f"{where} {item.url}")
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
