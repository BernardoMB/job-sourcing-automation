"""LinkedIn through linkedin-toolkit: reading threads and replying to the right person (publicId)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from steward import decisions
from steward.config import load_config
from steward.db import loads
from steward.linkedin_adapt import adapt_args, pick_upstream_tool
from steward.orchestrator import Steward

from .conftest import run

FAKE = Path(__file__).with_name("fake_linkedin_toolkit.py")

# The schemas linkedin-toolkit 2.1.1 actually publishes for the tools that matter (from its tool list).
TOOLKIT_TOOLS = {
    "linkedin_get_conversations": {"schema": {"properties": {"since": {}, "unreadOnly": {}, "count": {}},
                                              "required": []}},
    "linkedin_get_messages": {"schema": {"properties": {"threadId": {}, "since": {}}, "required": ["threadId"]}},
    "linkedin_send_message": {"schema": {"properties": {"publicId": {}, "body": {}, "dry_run": {}},
                                         "required": ["publicId", "body"]}},
    "linkedin_send_inmail": {"schema": {"properties": {"publicId": {}, "subject": {}, "body": {}},
                                        "required": ["publicId", "subject", "body"]}},
    "linkedin_queue_list": {"schema": {"properties": {"status": {}}, "required": []}},
}


def test_toolkit_tools_are_matched_by_what_they_need():
    m = {k: pick_upstream_tool(k, TOOLKIT_TOOLS, {}) for k in ("list_conversations", "get_conversation", "send_message")}
    # Before: "linkedin_get_messages" won the inbox listing on a name tie and failed for lack of a threadId.
    assert m == {"list_conversations": "linkedin_get_conversations", "get_conversation": "linkedin_get_messages",
                 "send_message": "linkedin_send_message"}
    send = adapt_args(TOOLKIT_TOOLS["linkedin_send_message"]["schema"],
                      {"recipient_public_id": "maria-lopez", "conversation_id": "t-1", "text": "Hi"})
    assert send == {"publicId": "maria-lopez", "body": "Hi"}


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_steward_runs_the_bridge_and_talks_to_it_over_http(workspace, tmp_path, monkeypatch):
    from steward import linkedin_bridge

    port = _free_port()
    state = tmp_path / "toolkit_sends.json"
    monkeypatch.setenv("FAKE_LI_STATE", str(state))
    monkeypatch.setenv("FAKE_LI_TOKEN", "pair-123")
    data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    data["profile"]["linkedin_url"] = "https://www.linkedin.com/in/alex-rivera-example/"
    data["linkedin"].update({
        "backend": "upstream",
        "upstream": {"url": f"http://127.0.0.1:{port}/mcp", "token_env": "LINKEDIN_TOOLKIT_TOKEN"},
        "manage_bridge": True,
        "bridge_command": [sys.executable, str(FAKE), "--http", str(port)],
    })
    workspace.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    try:
        # No pairing token yet: a clear message, nothing ingested.
        monkeypatch.delenv("LINKEDIN_TOOLKIT_TOKEN", raising=False)
        s = Steward(load_config(workspace))
        summary = run(s.cycle(force=True))
        assert "linkedin" not in summary["ingested"]
        assert any("pairing token" in e for e in summary["errors"]), summary["errors"]
        assert linkedin_bridge.status(s.cfg)["running"]  # Steward started the bridge itself

        # With the token: threads are read over HTTP and the reply goes to the person's publicId.
        monkeypatch.setenv("LINKEDIN_TOOLKIT_TOKEN", "pair-123")
        summary = run(s.cycle(force=True))
        assert summary["ingested"]["linkedin"] == 1, summary["errors"]
        draft = s.db.query_one("SELECT d.* FROM drafts d JOIN events e ON e.id = d.event_id WHERE e.source = 'linkedin'")
        decisions.stage(s.db, draft["id"], "approve")
        assert run(s.go())["executed"] == 1
        assert json.loads(state.read_text(encoding="utf-8"))[0]["publicId"] == "maria-lopez"

        # A wrong token is refused by the bridge.
        monkeypatch.setenv("LINKEDIN_TOOLKIT_TOKEN", "wrong")
        s.db.kv_set("last_poll:linkedin", None)
        assert any("401" in e or "Unauthorized" in e or "UNAUTHORIZED" in e
                   for e in run(s.cycle(force=True))["errors"])
        s.db.close()
    finally:
        if linkedin_bridge._process and linkedin_bridge._process.poll() is None:
            linkedin_bridge._process.kill()
        linkedin_bridge._process, linkedin_bridge._last_start = None, 0.0


def test_reads_threads_and_replies_to_the_person_via_the_toolkit(workspace, tmp_path):
    state = tmp_path / "toolkit_sends.json"
    data = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    data["profile"]["linkedin_url"] = "https://www.linkedin.com/in/alex-rivera-example/"
    data["linkedin"]["backend"] = "upstream"
    data["linkedin"]["upstream"] = {"command": sys.executable, "args": [str(FAKE)],
                                    "env": {"FAKE_LI_STATE": str(state)}, "tools": {}}
    workspace.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    s = Steward(load_config(workspace))

    summary = run(s.cycle(force=True))
    assert summary["ingested"]["linkedin"] == 1, summary["errors"]  # old (>7 days) and answered threads skipped
    ev = s.db.query_one("SELECT * FROM events WHERE source = 'linkedin'")
    assert ev["thread_key"] == "t-recent" and ev["sender_name"] == "Maria Lopez"
    assert ev["sender_address"] == "https://www.linkedin.com/in/maria-lopez/"
    assert ev["body_text"].startswith("Hi Alex! We're hiring")  # only what she wrote after your last message
    history = loads(ev["raw_json"])["history"]
    assert [m["from"] for m in history] == ["me"] and history[0]["text"].startswith("Hi Maria")

    draft = s.db.query_one("SELECT * FROM drafts WHERE event_id = ?", (ev["id"],))
    action = json.loads(draft["actions_json"])[-1]
    assert action["type"] == "send_linkedin" and action["payload"]["conversation_id"] == "t-recent"
    decisions.stage(s.db, draft["id"], "approve")
    result = run(s.go())
    assert result["executed"] == 1, result
    sends = json.loads(state.read_text(encoding="utf-8"))
    assert len(sends) == 1 and sends[0]["publicId"] == "maria-lopez"
    assert sends[0]["body"] == action["payload"]["text"]
    done = s.db.get_draft(draft["id"])
    assert done["status"] == "executed" and "linkedin-toolkit queue" in done["status_reason"]
    s.db.close()
