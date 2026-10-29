"""An expired Google Calendar sign-in never stops a reply from being written, and the item can be tried again."""

from __future__ import annotations

import json

import pytest

from steward.mcp_hub import MCPHub, MCPToolError

from .conftest import run

EXPIRED = "RefreshError: ('invalid_grant: Token has been expired or revoked.')"


@pytest.fixture()
def calendar_down(monkeypatch):
    real = MCPHub.call

    async def call(self, server, tool, args=None, *a, **kw):
        if server == "calendar":
            raise MCPToolError(f"calendar.{tool}: Error executing tool {tool}: {EXPIRED}")
        return await real(self, server, tool, args, *a, **kw)

    monkeypatch.setattr(MCPHub, "call", call)
    return monkeypatch


def _scheduling_drafts(steward) -> list[dict]:
    out = []
    for d in steward.db.query("SELECT * FROM drafts WHERE status = 'pending'"):
        sched = (json.loads(d["analysis_json"] or "{}") or {}).get("schedule") or {}
        if sched.get("calendar_error"):
            out.append(d)
    return out


def test_replies_are_still_written_when_the_calendar_sign_in_expired(steward, calendar_down):
    run(steward.cycle(force=True))
    failed = steward.db.query("SELECT * FROM drafts WHERE route_reasons_json LIKE '%could not process%'")
    assert failed == []
    waiting = _scheduling_drafts(steward)
    assert waiting, "an availability request should be drafted without times"
    for d in waiting:
        actions = json.loads(d["actions_json"])
        assert any(a["type"].startswith("send_") for a in actions)
        assert not any(a["type"] == "create_event" for a in actions)


def test_try_again_rewrites_the_reply_once_the_calendar_is_back(steward, calendar_down):
    run(steward.cycle(force=True))
    old = _scheduling_drafts(steward)[0]
    calendar_down.undo()
    res = run(steward.retry(old["id"]))
    assert res["draft_id"] and res["draft_id"] != old["id"]
    assert steward.db.get_draft(old["id"])["status"] == "superseded"
    new = steward.db.get_draft(res["draft_id"])
    sched = json.loads(new["analysis_json"])["schedule"]
    assert not sched.get("calendar_error") and sched["kind"] != "unknown"
