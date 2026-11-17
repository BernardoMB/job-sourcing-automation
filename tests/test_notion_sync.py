"""Notion events -> calendar: the sync engine, its log, and the calendar side (sandbox, fake scraper)."""

from __future__ import annotations

from datetime import date, timedelta

from steward import notion_scrape, notion_sync
from steward.mcp_hub import MCPHub
from steward.notion_sync import NeedsLogin, NotionItem, ScrapeResult

from .conftest import run, sandbox_json


def _day(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def _items() -> list[NotionItem]:
    return [
        NotionItem(id="aaaa1111", title="Design review", start=f"{_day(2)}T15:00", end=f"{_day(2)}T16:00",
                   all_day=False, workspace="Acme Team", database="Meetings", url="https://www.notion.so/aaaa1111"),
        NotionItem(id="bbbb2222", title="Offsite", start=_day(5), end=_day(6), all_day=True, workspace="Personal",
                   database="Plans", url="https://www.notion.so/bbbb2222"),
    ]


def _result(items: list[NotionItem]) -> ScrapeResult:
    return ScrapeResult(items=items, workspaces=[{"name": "Acme Team", "items": 1, "databases": 1},
                                                 {"name": "Personal", "items": 1, "databases": 1}])


def _sync(steward, scraper) -> dict:
    async def go():
        async with MCPHub(steward.cfg) as hub:
            return await notion_sync.sync(steward.cfg, steward.db, hub, reason="test", scraper=scraper)
    return run(go())


def _log(steward) -> str:
    return notion_sync.log_path(steward.cfg).read_text(encoding="utf-8")


def _notion_events(steward) -> list[dict]:
    return [e for e in sandbox_json(steward, "calendar.json")["events"] if e.get("source_key")]


def test_sync_adds_events_once_and_logs_them(steward):
    items = _items()
    res = _sync(steward, lambda: _result(items))
    assert res["status"] == "ok" and res["added"] == 2 and res["found"] == 2
    events = {e["title"]: e for e in _notion_events(steward)}
    assert set(events) == {"Design review", "Offsite"}
    assert events["Offsite"]["all_day"] and events["Offsite"]["start"] == _day(5)
    assert events["Offsite"]["end"] == _day(7)  # exclusive end date for all-day events
    assert not events["Design review"]["all_day"] and events["Design review"]["attendees"] == []
    assert "https://www.notion.so/aaaa1111" in events["Design review"]["description"]

    text = _log(steward)
    assert "START" in text and "WORKSPACE Acme Team: 1 database(s) with dates, 1 dated item(s)" in text
    assert text.count("ADDED") == 2 and '"Design review"' in text and "[Acme Team / Meetings]" in text
    assert ("SYNC OK   Notion sync through Playwright MCP succeeded: 2 workspace(s), 2 event(s) found, 2 added, "
            "0 updated, 0 unchanged") in text
    assert 'ADDED     new event on your calendar: "Design review"' in text

    # A second run never duplicates.
    res = _sync(steward, lambda: _result(items))
    assert res["added"] == 0 and res["unchanged"] == 2
    assert len(_notion_events(steward)) == 2
    assert _log(steward).count("ADDED") == 2


def test_sync_updates_changed_items_and_reports_gone_ones(steward):
    items = _items()
    _sync(steward, lambda: _result(items))
    moved = [NotionItem(**{**items[0].__dict__, "title": "Design review (moved)", "start": f"{_day(3)}T10:00",
                           "end": f"{_day(3)}T11:00"})]
    res = _sync(steward, lambda: _result(moved))
    assert res["updated"] == 1 and res["added"] == 0
    text = _log(steward)
    assert 'UPDATED   "Design review (moved)"' in text and "changed: " in text
    assert 'GONE      "Offsite"' in text
    assert len(_notion_events(steward)) == 2  # nothing is ever deleted from the calendar


def test_event_you_deleted_is_not_re_added(steward):
    items = _items()
    _sync(steward, lambda: _result(items))
    path = steward.cfg.sandbox_path / "calendar.json"
    data = sandbox_json(steward, "calendar.json")
    for e in data["events"]:
        if e.get("title") == "Offsite":
            e["status"] = "cancelled"
    import json

    path.write_text(json.dumps(data), encoding="utf-8")
    res = _sync(steward, lambda: _result(items))
    assert res["added"] == 0
    assert "SKIPPED" in _log(steward) and "you deleted it from your calendar" in _log(steward)


def test_needs_login_and_failures_are_logged(steward):
    def signed_out():
        raise NeedsLogin("nobody signed in within 15 minutes")

    res = _sync(steward, signed_out)
    assert res["status"] == "needs_login"
    assert "NEEDS LOGIN not signed in to Notion: nobody signed in within 15 minutes" in _log(steward)

    def broken():
        raise RuntimeError("Notion did not load")

    res = _sync(steward, broken)
    assert res["status"] == "failed"
    assert ("SYNC FAILED Notion sync through Playwright MCP failed: could not read Notion: RuntimeError: "
            "Notion did not load") in _log(steward)
    assert notion_sync.last_status(steward.cfg, steward.db)["status"] == "failed"


def test_due_and_item_windows(steward):
    assert notion_sync.due(steward.cfg, steward.db)
    _sync(steward, lambda: _result([]))
    assert not notion_sync.due(steward.cfg, steward.db)
    timed = NotionItem(id="x", title="t", start="2026-11-18T15:00", end="", all_day=False,
                       time_zone="America/Mexico_City")
    s, e = timed.window("UTC")
    assert s.startswith("2026-11-18T21:00") and e.startswith("2026-11-18T22:00")
    assert timed.label("UTC") == "2026-11-18 15:00-16:00 (America/Mexico_City)"
    assert timed.label("America/Mexico_City") == "2026-11-18 15:00-16:00"
    assert NotionItem(id="y", title="d", start="2026-11-18").window("UTC") == ("2026-11-18", "2026-11-18")


def test_date_parsing_from_notion_records():
    tzname = "America/Mexico_City"
    d = notion_scrape.parse_date_value(
        [["‣", [["d", {"type": "datetimerange", "start_date": "2026-11-18", "start_time": "15:00",
                       "end_date": "2026-11-18", "end_time": "16:30", "time_zone": "Europe/Madrid"}]]]])
    assert d == {"start": "2026-11-18T15:00", "end": "2026-11-18T16:30", "all_day": False, "time_zone": "Europe/Madrid"}
    d = notion_scrape.parse_date_value([["‣", [["d", {"type": "date", "start_date": "2026-11-20"}]]]])
    assert d == {"start": "2026-11-20", "end": "", "all_day": True, "time_zone": ""}
    assert notion_scrape.parse_date_value([["plain text"]]) is None
    assert notion_scrape.plain_text([["Design ", []], ["review", [["b"]]]]) == "Design review"
    assert tzname  # the profile time zone is applied later, in NotionItem.window


def test_date_property_choice():
    schema = {"a": {"name": "Created", "type": "created_time"}, "b": {"name": "Due Date", "type": "date"},
              "c": {"name": "Follow-up", "type": "date"}, "t": {"name": "Name", "type": "title"}}
    assert notion_scrape.date_property(schema, [{"type": "table"}], False) == "b"
    assert notion_scrape.date_property(schema, [{"type": "table"}], True) == ""
    cal = {"type": "calendar", "format": {"calendar_by": "c"}}
    assert notion_scrape.date_property(schema, [{"type": "table"}, cal], True) == "c"
    assert notion_scrape.date_property({"t": {"name": "Name", "type": "title"}}, [], False) == ""


class FakeNotion:
    """Answers the Notion web-app endpoints collect() uses, like app.notion.com does."""

    def __init__(self):
        today = date.today()
        self.rows = {
            "row-soon": {"id": "row-soon", "alive": True, "properties": {
                "title": [["Interview with Acme"]],
                "dp": [["‣", [["d", {"type": "datetime", "start_date": (today + timedelta(days=1)).isoformat(),
                                     "start_time": "10:00"}]]]]}},
            "row-past": {"id": "row-past", "alive": True, "properties": {
                "title": [["Old"]], "dp": [["‣", [["d", {"type": "date",
                                                         "start_date": (today - timedelta(days=9)).isoformat()}]]]]}},
            "row-nodate": {"id": "row-nodate", "alive": True, "properties": {"title": [["No date"]]}},
        }

    async def api(self, page, endpoint, body, user_id="", space_id=""):
        wrap = lambda d: {k: {"value": {"value": v, "role": "editor"}} for k, v in d.items()}  # noqa: E731
        if endpoint == "getSpaces":
            return {"u1": {"space": wrap({"s1": {"id": "s1", "name": "JOB2US"}}),
                           "space_view": wrap({"sv1": {"id": "sv1", "space_id": "s1", "alive": True,
                                                       "private_pages": ["p1"]}}),
                           "block": {}}}
        if endpoint in ("getUserSharedPagesInSpace",):
            return {"pages": []}
        if endpoint == "getTeamsV2":
            return {"teams": [], "recordMap": {}}
        if endpoint == "loadCachedPageChunkV2":
            pid = body["page"]["id"]
            if pid == "p1":
                return {"cursor": {"stack": []}, "recordMap": {"block": wrap({
                    "p1": {"id": "p1", "type": "page", "space_id": "s1", "alive": True, "content": ["db1", "p2"]},
                    "db1": {"id": "db1", "type": "collection_view", "space_id": "s1", "alive": True,
                            "collection_id": "c1", "view_ids": ["v1"], "parent_table": "block"},
                    "p2": {"id": "p2", "type": "page", "space_id": "s1", "alive": True, "parent_table": "block"}}),
                    "collection": wrap({"c1": {"id": "c1", "alive": True, "name": [["Interviews"]],
                                               "schema": {"title": {"name": "Name", "type": "title"},
                                                          "dp": {"name": "Date & Time", "type": "date"}}}}),
                    "collection_view": wrap({"v1": {"id": "v1", "type": "board"}})}}
            return {"cursor": {"stack": []}, "recordMap": {"block": wrap({pid: {"id": pid, "type": "page"}})}}
        if endpoint == "queryCollection":
            assert body["collection"]["id"] == "c1" and body["collectionView"]["id"] == "v1"
            return {"result": {"reducerResults": {"collection_group_results": {
                "type": "results", "blockIds": list(self.rows), "hasMore": False}}},
                "recordMap": {"block": wrap(self.rows)}}
        raise AssertionError(f"unexpected endpoint {endpoint}")


def test_collect_walks_workspaces_and_reads_dated_rows():
    result = ScrapeResult()
    run(notion_scrape.collect(FakeNotion(), None, result, 60, False, "America/Mexico_City"))
    assert [w["name"] for w in result.workspaces] == ["JOB2US"]
    assert result.workspaces[0]["databases"] == 1 and result.workspaces[0]["pages"] == 2
    assert [(i.title, i.database, i.all_day) for i in result.items] == [("Interview with Acme", "Interviews", False)]
    assert result.items[0].url == "https://app.notion.com/rowsoon"  # Notion page links drop the dashes


def test_dashboard_notion_endpoints(workspace, monkeypatch):
    from fastapi.testclient import TestClient

    from steward.config import load_config
    from steward.orchestrator import Steward
    from steward.web.app import create_app

    calls = []

    async def fake_sync(self, interactive=True, reason="scheduled"):
        calls.append((interactive, reason))
        notion_sync.SyncLog(notion_sync.log_path(self.cfg)).write("SYNC OK", "fake run")
        return {"status": "ok", "detail": "fake run", "added": 0, "added_titles": []}

    monkeypatch.setattr(Steward, "sync_notion_events", fake_sync)
    cfg = load_config(workspace)
    app = create_app(cfg, watch=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        assert client.post("/api/setup/notion/sync", headers=h).status_code == 400  # not switched on yet
        assert client.post("/api/setup/config", json={"changes": {"notion.enabled": True}}, headers=h).status_code == 200
        assert client.post("/api/setup/notion/sync").status_code == 403  # token required
        assert client.post("/api/setup/notion/sync", headers=h).json()["started"] is True
        for _ in range(50):
            if calls and not client.get("/api/notion/status").json()["running"]:
                break
            import time

            time.sleep(0.05)
        assert calls == [(True, "Sync now")]
        log = client.get("/api/notion/log").json()
        assert "SYNC OK   fake run" in log["text"] and log["path"].endswith("notion-sync.log")
        assert client.post("/api/setup/notion/forget", headers=h).json() == {"ok": True, "removed": False}


def test_setup_step_reports_notion(steward):
    from steward import setup as setup_mod

    check = next(c for c in setup_mod.checks(steward.cfg) if c["id"] == "notion")
    assert check["status"] == "off"
    steward.cfg.notion.enabled = True
    _sync(steward, lambda: _result(_items()))
    check = setup_mod.notion_check(steward.cfg)
    assert check["status"] in ("ok", "todo")  # todo only when Node.js (npx) is not installed


# -- the Playwright MCP browser ----------------------------------------------------------
def test_parse_browser_evaluate_result():
    text = ('### Result\n{\n  "status": 200,\n  "data": {"a": "x\\n### not a section"}\n}\n'
            "### Ran Playwright code\n```js\nawait page.evaluate('...');\n```\n### Page\n- Page URL: https://app.notion.com/")
    assert notion_sync.parse_result(text) == {"status": 200, "data": {"a": "x\n### not a section"}}
    assert notion_sync.parse_result("### Result\n\"about:blank\"") == "about:blank"
    assert notion_sync.parse_result("### Result\nundefined\n### Ran Playwright code") is None


def test_playwright_mcp_server_config(steward):
    import json

    mcp = notion_sync.PlaywrightMCP(steward.cfg, headless=True)
    conf = json.loads(mcp._config_file().read_text(encoding="utf-8"))["browser"]
    assert conf["launchOptions"]["headless"] is True and conf["launchOptions"]["channel"] == "msedge"
    assert conf["userDataDir"] == str(notion_sync.profile_path(steward.cfg))
    login = json.loads(notion_sync.PlaywrightMCP(steward.cfg, headless=False)._config_file()
                       .read_text(encoding="utf-8"))["browser"]
    assert login["launchOptions"]["headless"] is False and login["userDataDir"] == conf["userDataDir"]
    assert "@playwright/mcp@" in mcp.describe() and "headless" in mcp.describe()


class FakePlaywrightMCP:
    """Stands in for the Playwright MCP server: records each start, headless or visible."""

    starts: list[bool] = []
    signed_in_profile = True
    login_succeeds = True

    def __init__(self, cfg, headless):
        self.headless = headless

    def describe(self):
        return f"@playwright/mcp@test, msedge, {'headless' if self.headless else 'visible window'}"

    async def __aenter__(self):
        FakePlaywrightMCP.starts.append(self.headless)
        return self

    async def __aexit__(self, *exc):
        return None

    async def navigate(self, url):
        self.url = url

    async def signed_in(self):
        if not self.headless and FakePlaywrightMCP.login_succeeds:
            FakePlaywrightMCP.signed_in_profile = True  # you signed in in the window
        if not self.headless and not FakePlaywrightMCP.login_succeeds:
            return False, "about:blank"  # you closed the window
        return FakePlaywrightMCP.signed_in_profile, "https://app.notion.com/robots.txt"

    async def api(self, page, endpoint, body, user_id="", space_id=""):
        return await FakeNotion().api(page, endpoint, body, user_id, space_id)


def _scrape(steward, monkeypatch, *, signed_in, login_succeeds=True, interactive=True):
    async def no_wait(_):
        return None

    monkeypatch.setattr(notion_sync, "PlaywrightMCP", FakePlaywrightMCP)
    monkeypatch.setattr(notion_sync.asyncio, "sleep", no_wait)
    FakePlaywrightMCP.starts = []
    FakePlaywrightMCP.signed_in_profile = signed_in
    FakePlaywrightMCP.login_succeeds = login_succeeds
    browser = notion_sync.NotionBrowser(steward.cfg, notion_sync.SyncLog(notion_sync.log_path(steward.cfg)))
    return run(browser.scrape(interactive, 60, False))


def test_signed_in_runs_headless_only(steward, monkeypatch):
    res = _scrape(steward, monkeypatch, signed_in=True)
    assert FakePlaywrightMCP.starts == [True]  # one headless server; no window ever opened
    assert [i.title for i in res.items] == ["Interview with Acme"] and not res.logged_in_now
    text = _log(steward)
    assert "BROWSER   Playwright MCP server started (@playwright/mcp@test, msedge, headless); no window is shown" in text
    assert "LOGIN" not in text


def test_signed_out_opens_login_window_then_continues_headless(steward, monkeypatch):
    res = _scrape(steward, monkeypatch, signed_in=False)
    assert FakePlaywrightMCP.starts == [True, False, True]  # check headless, sign-in window, back to headless
    assert res.logged_in_now and len(res.items) == 1
    text = _log(steward)
    assert "LOGIN     not signed in to Notion: opening a browser window (Playwright MCP)" in text
    assert "LOGIN     signed in; session saved in the browser profile; window closed" in text


def test_signed_out_without_login_window(steward, monkeypatch):
    import pytest

    with pytest.raises(NeedsLogin, match="signed out"):
        _scrape(steward, monkeypatch, signed_in=False, interactive=False)
    assert FakePlaywrightMCP.starts == [True]
    with pytest.raises(NeedsLogin, match="window was closed"):
        _scrape(steward, monkeypatch, signed_in=False, login_succeeds=False)
    assert FakePlaywrightMCP.starts == [True, False]
