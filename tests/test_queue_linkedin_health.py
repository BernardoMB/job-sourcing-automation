"""Decisions waiting for Go in the Queue tab, and the LinkedIn check at startup."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from steward import activity, linkedin_bridge, linkedin_health
from steward.config import load_config
from steward.orchestrator import Steward
from steward.web.app import create_app

from .conftest import run

FAKE = Path(__file__).with_name("fake_linkedin_toolkit.py")


def _client(workspace):
    client = TestClient(create_app(load_config(workspace), watch=False), base_url="http://127.0.0.1:8765")
    return client


def test_decisions_wait_in_the_queue_as_pending_confirmation(workspace):
    with _client(workspace) as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        h = {"X-Steward-Token": token}
        client.post("/api/poll", headers=h)
        b = client.get("/api/briefing").json()
        clean = [v for v in b["needs_decision"] + b["high_priority"] if "[NEEDS YOUR INPUT" not in str(v["actions"])]
        approve, reject = clean[0]["id"], clean[1]["id"]
        client.post(f"/api/drafts/{approve}/decision", json={"decision": "approve"}, headers=h)
        client.post(f"/api/drafts/{reject}/decision", json={"decision": "reject"}, headers=h)

        q = {x["id"]: x for x in client.get("/api/queue").json()["items"]}
        assert q[approve]["status_label"] == q[reject]["status_label"] == "Pending confirmation"  # both, before Go
        assert q[approve]["actions"] and q[reject]["actions"] == []
        assert "Press Go" in q[approve]["reason"] and "nothing will be sent" in q[reject]["reason"]
        blockers = client.get("/api/logs").json()["blockers"]
        assert any(x["title"] == "2 decisions pending confirmation" and x.get("go") for x in blockers)
        assert client.get("/api/briefing").json()["counts"]["staged"] == 2  # the Queue badge counts these

        client.post("/api/go", headers=h)  # Go: they start processing and leave "Pending confirmation"
        q = {x["id"]: x for x in client.get("/api/queue").json()["items"]}
        assert q[approve]["status_label"] != "Pending confirmation" and reject not in q


def _upstream(workspace, tmp_path, monkeypatch, **env) -> Steward:
    data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    data["linkedin"]["backend"] = "upstream"
    data["linkedin"]["upstream"] = {"command": sys.executable, "args": [str(FAKE)],
                                    "env": {"FAKE_LI_STATE": str(tmp_path / "sends.json"), **env}, "tools": {}}
    workspace.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return Steward(load_config(workspace))


def test_linkedin_is_checked_at_startup_and_logged(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(linkedin_bridge, "extension_patched", lambda: True)
    s = _upstream(workspace, tmp_path, monkeypatch)
    res = run(linkedin_health.check(s, "startup"))
    assert res["read_ok"] and res["can_send"] and res["problems"] == []
    [line] = [e for e in activity.recent(s.db) if e["kind"] == "linkedin"]
    assert line["level"] == "info" and line["message"].startswith("LinkedIn check (startup): reading works (")
    assert "sending ready (Autopilot" in line["message"] and "38 of 40 messages left today" in line["message"]
    assert "with the send fix" in line["message"] and "invites 0/20" in line["message"]
    s.db.close()


def test_linkedin_problems_are_logged_and_block(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(linkedin_bridge, "extension_patched", lambda: False)
    s = _upstream(workspace, tmp_path, monkeypatch, FAKE_LI_SIGNED_OUT="1", FAKE_LI_MODE="copilot")
    res = run(linkedin_health.check(s, "startup"))
    assert res["read_ok"] and not res["can_send"]
    log = [e for e in activity.recent(s.db) if e["kind"] == "linkedin"]
    summary = log[-1]["message"]
    assert "sending NOT ready (Copilot" in summary
    problems = [e["message"] for e in log[:-1]]
    assert any("signed out in Edge" in p for p in problems) and any("apply patches/linkedin-toolkit-2.1.1" in p
                                                                    for p in problems)
    from steward import work_queue
    assert any(b.get("linkedin") for b in work_queue.blockers(s.cfg, s.db))
    s.db.close()


def test_check_now_button_and_sandbox(workspace):
    with _client(workspace) as client:
        token = client.get("/").text.split('const TOKEN = "')[1].split('"')[0]
        assert client.post("/api/linkedin/check").status_code == 403  # needs the page token
        res = client.post("/api/linkedin/check", headers={"X-Steward-Token": token}).json()
        assert res["backend"] == "sandbox"
        msgs = [e["message"] for e in client.get("/api/logs").json()["entries"]]
        expected = "LinkedIn check (you pressed Check LinkedIn now): LinkedIn is sandbox (demo data)."
        assert expected in msgs
