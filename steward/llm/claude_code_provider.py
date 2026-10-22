"""Claude through your Claude Pro/Max subscription, by running the official Claude Code program.

Job Sourcer never reads, stores, or forwards your Claude login. It runs the
unmodified `claude` program in non-interactive mode (`claude -p`), exactly as
you could from a terminal, so the usage counts against your own plan. Each call:

- replaces Claude Code's system prompt with Job Sourcer's (`--system-prompt`),
- has no tools at all (`--tools ""`), so a recruiter's message can't make it act,
- loads none of your Claude Code settings, hooks, plugins, or MCP servers,
- saves no session history (`--no-session-persistence`),
- returns JSON validated against Job Sourcer's schema (`--json-schema`).

When your plan's usage limit is reached, calls raise `LLMUnavailable` with the
reset time; Job Sourcer keeps new messages queued and resumes after the reset.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from steward.config import LLMConfig
from steward.llm.base import LLMError, LLMRefusal, LLMUnavailable, extract_json

# Environment variables that configure the Claude Code program itself and are safe to pass on.
_KEEP_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CONFIG_DIR"}


def find_claude(configured: str = "") -> str:
    candidates = [configured, shutil.which("claude") or ""]
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(local, "Microsoft", "WinGet", "Links", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
        *sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Anthropic.ClaudeCode_*", "claude*.exe"))),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def clean_env() -> dict[str, str]:
    """The environment of a normal terminal: no API key (so your subscription is used), no host-session vars."""
    return {k: v for k, v in os.environ.items()
            if not (k.upper().startswith(("CLAUDE", "ANTHROPIC_")) and k.upper() not in _KEEP_ENV)}


def auth_status(configured_path: str = "") -> dict[str, Any]:
    """Ask Claude Code whether it is signed in (no secrets are involved or returned)."""
    exe = find_claude(configured_path)
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        p = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           env=clean_env(), timeout=60)
        data = json.loads(p.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"installed": True, "loggedIn": False, "error": str(exc), "path": exe}
    keep = ("loggedIn", "authMethod", "email", "subscriptionType")
    return {"installed": True, "path": exe, **{k: data.get(k) for k in keep}}


def _reset_time(text: str) -> datetime | None:
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        local_now = datetime.now().astimezone()
        reset = local_now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if reset <= local_now:
            reset += timedelta(days=1)
        return reset.astimezone(timezone.utc)
    return None


class ClaudeCodeLLM:
    name = "claude-code"

    def __init__(self, cfg: LLMConfig, workdir: Path):
        self.cfg = cfg
        self.exe = find_claude(cfg.claude_code_path)
        if not self.exe:
            raise LLMUnavailable(
                "Claude Code is not installed. Install it (winget install Anthropic.ClaudeCode), run `claude` once "
                "and sign in with your Claude account.", datetime.now(timezone.utc) + timedelta(minutes=30))
        # An empty folder: no CLAUDE.md, no project settings are picked up.
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)

    def model_for(self, purpose: str) -> str:
        return self.cfg.models.get(purpose) or self.cfg.claude_code_model

    def json(self, purpose: str, system: str, user: str, schema: dict[str, Any],
             context: dict[str, Any] | None = None) -> dict[str, Any]:
        args = [self.exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                "--disable-slash-commands", "--setting-sources", "project"]
        model = self.model_for(purpose)
        if model:
            args += ["--model", model]
        effort = self.cfg.effort.get(purpose)
        if effort:
            args += ["--effort", effort]
        try:
            proc = subprocess.run(args, input=user, capture_output=True, text=True, encoding="utf-8",
                                  cwd=self.workdir, env=clean_env(), timeout=self.cfg.claude_code_timeout)
        except subprocess.TimeoutExpired as exc:
            raise LLMUnavailable(f"Claude Code did not answer within {self.cfg.claude_code_timeout}s",
                                 datetime.now(timezone.utc) + timedelta(minutes=2)) from exc
        except OSError as exc:
            raise LLMUnavailable(f"Could not run Claude Code: {exc}",
                                 datetime.now(timezone.utc) + timedelta(minutes=10)) from exc
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = {}
        text = str(data.get("result") or proc.stdout or proc.stderr or "")
        if not data or data.get("is_error") or data.get("subtype", "success") != "success":
            low = text.lower()
            if "limit" in low and any(w in low for w in ("usage", "reached", "hit", "resets")):
                retry = _reset_time(text) or datetime.now(timezone.utc) + timedelta(minutes=30)
                raise LLMUnavailable(f"Your Claude plan's usage limit is reached: {text.strip()[:200]}", retry)
            if any(w in low for w in ("/login", "not logged in", "log in", "invalid api key", "oauth", "authentication")):
                raise LLMUnavailable("Claude Code is not signed in. Open a terminal, run `claude`, type /login and "
                                     "choose your Claude account.", datetime.now(timezone.utc) + timedelta(minutes=10))
            if "refus" in low or data.get("stop_reason") == "refusal":
                raise LLMRefusal(f"Claude declined this {purpose} request")
            if "overloaded" in low or "529" in low or "network" in low or "connection" in low:
                raise LLMUnavailable(f"Claude is temporarily unavailable: {text.strip()[:200]}",
                                     datetime.now(timezone.utc) + timedelta(minutes=3))
            raise LLMError(f"Claude Code failed on the {purpose} request: {text.strip()[:300]}")
        structured = data.get("structured_output")
        if isinstance(structured, dict):
            return structured
        return extract_json(text)
