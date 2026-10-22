"""Claude via your subscription (Claude Code): invocation, error handling, and usage-limit pauses.

These tests use a fake `claude` program; they never spend your plan's usage.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from steward.config import LLMConfig
from steward.llm import claude_code_provider as ccp
from steward.llm.base import LLMError, LLMUnavailable
from steward.timeutil import utcnow

from .conftest import run


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")
    calls: list[dict] = []
    replies: list[dict] = []

    def fake_run(args, **kw):
        calls.append({"args": args, **kw})
        reply = replies.pop(0) if replies else {"type": "result", "subtype": "success", "is_error": False,
                                                "structured_output": {"ok": True}, "result": "{}"}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(reply), stderr="")

    monkeypatch.setattr(ccp.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    llm = ccp.ClaudeCodeLLM(LLMConfig(provider="claude-code", claude_code_path=str(exe)), tmp_path / "cwd")
    return llm, calls, replies


def test_runs_claude_code_isolated_with_your_subscription(fake_claude):
    llm, calls, _ = fake_claude
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    assert llm.json("draft", "SYSTEM", "USER MESSAGE", schema) == {"ok": True}
    call = calls[0]
    args = call["args"]
    assert args[1] == "-p" and args[args.index("--json-schema") + 1] == json.dumps(schema)
    assert args[args.index("--tools") + 1] == ""  # no tools: a recruiter's message cannot make it act
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--system-prompt") + 1] == "SYSTEM"
    assert args[args.index("--model") + 1] == "opus" and args[args.index("--effort") + 1] == "medium"
    assert call["input"] == "USER MESSAGE"
    # Your plan is used, not an API key, and the host session's variables don't leak in.
    assert "ANTHROPIC_API_KEY" not in call["env"] and "CLAUDECODE" not in call["env"]
    assert Path(call["cwd"]).name == "cwd"


def test_usage_limit_and_login_errors_pause_instead_of_failing(fake_claude):
    llm, _, replies = fake_claude
    reset = int((utcnow() + timedelta(hours=2)).timestamp())
    replies.append({"type": "result", "subtype": "success", "is_error": True,
                    "result": f"Claude AI usage limit reached|{reset}"})
    with pytest.raises(LLMUnavailable) as exc:
        llm.json("classify", "S", "U", {})
    assert abs(exc.value.retry_at.timestamp() - reset) < 1
    replies.append({"type": "result", "subtype": "success", "is_error": True,
                    "result": "Invalid API key · Please run /login"})
    with pytest.raises(LLMUnavailable, match="not signed in"):
        llm.json("classify", "S", "U", {})
    replies.append({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"})
    with pytest.raises(LLMError):
        llm.json("classify", "S", "U", {})


class _LimitedLLM:
    """Pretends the plan's usage limit is reached for the next hour."""

    name = "limited"

    def __init__(self):
        self.calls = 0

    def model_for(self, purpose):
        return "opus"

    def json(self, purpose, system, user, schema, context=None):
        self.calls += 1
        raise LLMUnavailable("usage limit reached", utcnow() + timedelta(hours=1))


def test_queue_waits_for_the_reset_without_losing_messages(steward):
    limited = _LimitedLLM()
    steward._llm = limited
    summary = run(steward.cycle(force=True))
    assert summary["processed"] == 0 and limited.calls == 1  # stops at the first refusal of service
    assert any("unavailable until" in e for e in summary["errors"])
    waiting = steward.db.query("SELECT attempts FROM events WHERE status = 'new'")
    assert waiting and all(r["attempts"] == 0 for r in waiting)  # nothing counted as a failure
    run(steward.cycle(force=True))
    assert limited.calls == 1  # still paused: no calls burned while the limit lasts

    # The limit resets: everything queued is drafted.
    steward.db.kv_set("llm_paused", {"until": (utcnow() - timedelta(minutes=1)).isoformat(), "reason": "x"})
    steward._llm = None
    steward.cfg.llm.provider = "offline"
    assert run(steward.cycle(force=True))["processed"] == 10
