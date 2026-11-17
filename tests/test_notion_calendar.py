"""Scheduled entries in your Notion workspaces go on your calendar, and the Setup tab connects the workspaces."""

from __future__ import annotations

import os
from datetime import timedelta

import yaml
from fastapi.testclient import TestClient

from steward import activity
from steward.config import load_config
from steward.mcp_hub import MCPHub
from steward.servers.notion import entries_from_page
from steward.timeutil import parse_iso, to_iso, utcnow
from steward.web.app import create_app

from .conftest import run, sandbox_json


def _page(page_id: str, title: str, start: str, end: str | None = None, tz: str | None = None) -> dict:
    return {"id": page_id, "url": f"https://www.notion.so/{page_id}", "properties": {
        "Name": {"id": "title", "type": "title", "title": [{"plain_text": title}]},
        "When": {"id": "abc", "type": "date", "date": {"start": start, "end": end, "time_zone": tz}},
        "Due": {"id": "def", "type": "date", "date": {"start": start[:10], "end": None, "time_zone": None}},
    }}


def test_reading_dates_from_a_notion_row():
    [e], date_only = entries_from_page(_page("p1", "Dentist", "2026-11-20T10:00:00.000-06:00",
                                             "2026-11-20T10:45:00.000-06:00"), "Personal", "Home", "UTC", 60)
    assert (e["title"], e["start"], e["end"], date_only) == (
        "Dentist", "2026-11-20T10:00:00-06:00", "2026-11-20T10:45:00-06:00", 1)  # a date without a time is skipped
    # A separate time zone, and no end: the default length.
    [e], _ = entries_from_page(_page("p2", "Call", "2026-11-20T09:00:00.000", tz="America/New_York"),
                               "Work", "Office", "UTC", 60)
    assert parse_iso(e["start"]) == parse_iso("2026-11-20T14:00:00+00:00")
    assert parse_iso(e["end"]) - parse_iso(e["start"]) == timedelta(hours=1)
    assert e["uid"] == "notion-p2-abc@jobsourcer"


def _notion_rows(monkeypatch, rows: list[dict]):
    real = MCPHub.call

    async def call(self, server, tool, args=None, *a, **kw):
        if server == "notion" and tool == "scheduled_entries":
            return {"workspace": "Home", "databases": 1, "entries": list(rows), "date_only": 0}
        return await real(self, server, tool, args, *a, **kw)

    monkeypatch.setattr(MCPHub, "call", call)


def test_notion_entries_go_on_the_calendar_and_follow_changes(steward, monkeypatch):
    steward.cfg.notion.enabled = True
    steward.cfg.notion.database_id = ""
    monkeypatch.setenv(steward.cfg.notion.token_env, "ntn_test")
    start = (utcnow() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    row = {"uid": "notion-p9-abc@jobsourcer", "title": "Dentist", "start": to_iso(start),
           "end": to_iso(start + timedelta(minutes=45)), "url": "https://www.notion.so/p9", "database": "Personal",
           "workspace": "Home", "property": "When"}
    rows = [row]
    _notion_rows(monkeypatch, rows)

    async def sync():
        async with MCPHub(steward.cfg) as hub:
            return await steward.sync_notion_calendar(hub)

    assert run(sync()) == 1
    copies = [e for e in sandbox_json(steward, "calendar.json")["events"] if e.get("ical_uid") == row["uid"]]
    assert len(copies) == 1 and copies[0]["title"] == "Dentist" and copies[0]["attendees"] == []
    assert any("From Notion (Home / Personal): Dentist" in a["message"] for a in activity.recent(steward.db))

    assert run(sync()) == 0  # unchanged: nothing added twice
    later = start + timedelta(days=1)
    rows[0] = {**row, "start": to_iso(later), "end": to_iso(later + timedelta(minutes=45))}
    assert run(sync()) == 1  # moved in Notion: moved on the calendar
    copies = [e for e in sandbox_json(steward, "calendar.json")["events"] if e.get("ical_uid") == row["uid"]]
    assert len(copies) == 1 and parse_iso(copies[0]["start"]) == later

    # You deleted the copy from your calendar: it is not added back.
    path = steward.cfg.sandbox_path / "calendar.json"
    data = sandbox_json(steward, "calendar.json")
    data["events"] = [e for e in data["events"] if e.get("ical_uid") != row["uid"]]
    path.write_text(__import__("json").dumps(data), encoding="utf-8")
    assert run(sync()) == 0
    assert not [e for e in sandbox_json(steward, "calendar.json")["events"] if e.get("ical_uid") == row["uid"]]


def test_setup_connects_more_notion_workspaces(workspace, monkeypatch):
    for name in ("NOTION_TOKEN", "NOTION_TOKEN_2"):
        monkeypatch.delenv(name, raising=False)
    seen = []

    async def call(self, server, tool, args=None, *a, **kw):
        assert (server, tool) == ("notion", "workspace_status")
        token = os.environ.get(args["token_env"], "")
        seen.append(args["token_env"])
        if token == "bad":
            raise RuntimeError("Notion API 401: unauthorized")
        return {"workspace": {"ntn_a": "Home", "ntn_b": "Work"}[token],
                "databases": [{"id": "d1", "title": "Calendar", "date_properties": ["When"]}]}

    monkeypatch.setattr(MCPHub, "call", call)
    app = create_app(load_config(workspace), watch=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        r = client.post("/api/setup/notion-workspace", json={"token": "bad"}, headers=h)
        assert r.status_code == 400 and "did not accept" in r.text and not os.environ.get("NOTION_TOKEN")
        assert client.post("/api/setup/notion-workspace", json={"token": "ntn_a"}, headers=h).json()["env"] == "NOTION_TOKEN"
        assert client.post("/api/setup/notion-workspace", json={"token": "ntn_b"}, headers=h).json()["env"] == "NOTION_TOKEN_2"
        ws = client.get("/api/setup/notion-workspaces", headers=h).json()["workspaces"]
        assert [(w["workspace"], w["with_dates"]) for w in ws] == [("Home", ["Calendar"]), ("Work", ["Calendar"])]
        data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
        assert data["notion"]["enabled"] and data["notion"]["extra_token_envs"] == ["NOTION_TOKEN_2"]
        env_text = (workspace.parent / ".env").read_text(encoding="utf-8")
        assert "NOTION_TOKEN=ntn_a" in env_text and "NOTION_TOKEN_2=ntn_b" in env_text
        client.post("/api/setup/notion-workspace/remove", json={"env": "NOTION_TOKEN_2"}, headers=h)
        assert [w["workspace"] for w in client.get("/api/setup/notion-workspaces", headers=h).json()["workspaces"]] == ["Home"]
