"""MCP client hub: every integration (email, calendar, CV, LinkedIn, Notion) is an MCP server.

Each server runs as its own stdio subprocess and receives only its own
credentials. Servers start lazily on first use. If one server fails, the
others keep working.

Safety invariant, enforced here in code: tools that act on the outside world
(send an email, send a LinkedIn message, book or answer a meeting) can only be
called with the executor's capability token. The reasoning subagents never get
it, so the LLM can draft but can never send.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from steward.config import Config, ServerSpec

BUILTIN_MODULES = {
    "mail": "steward.servers.mail",
    "calendar": "steward.servers.calendar",
    "cv": "steward.servers.cv",
    "linkedin": "steward.servers.linkedin",
    "notion": "steward.servers.notion",
}

# Outward-facing tools: executor only.
EXECUTOR_ONLY = {
    ("mail", "send_email"),
    ("calendar", "create_event"),
    ("calendar", "rsvp_event"),
    ("linkedin", "send_message"),
}


class ExecutorCapability:
    """Possession of an instance is what lets a caller use outward-facing tools.

    Only `steward.executor.Executor` creates one; subagents are never handed it.
    """


class MCPToolError(RuntimeError):
    pass


class IntegrationDisabled(MCPToolError):
    pass


class MCPHub:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._failed: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "MCPHub":
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    # -- configuration --------------------------------------------------
    def enabled(self, name: str) -> bool:
        c = self.cfg
        return {
            "mail": c.mail.backend != "disabled",
            "calendar": c.calendar.backend != "disabled",
            "linkedin": c.linkedin.backend != "disabled",
            "notion": c.notion.enabled,
            "cv": True,
        }.get(name, False)

    def _spec(self, name: str) -> ServerSpec | None:
        section = getattr(self.cfg, name, None)
        return getattr(section, "server", None) if section is not None else None

    def _secrets_for(self, name: str) -> dict[str, str]:
        keys: list[str] = []
        if name == "mail":
            keys.append(self.cfg.mail.password_env)
        elif name == "notion":
            keys.append(self.cfg.notion.token_env)
        return {k: os.environ[k] for k in keys if k in os.environ}

    def _params(self, name: str) -> StdioServerParameters:
        project_root = str(Path(__file__).resolve().parent.parent)
        env = {
            **get_default_environment(),
            "STEWARD_CONFIG": self.cfg.config_path,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(p for p in [project_root, os.environ.get("PYTHONPATH", "")] if p),
            **self._secrets_for(name),
        }
        spec = self._spec(name)
        if spec is not None:
            return StdioServerParameters(command=spec.command, args=spec.args, env={**env, **spec.env})
        return StdioServerParameters(command=sys.executable, args=["-m", BUILTIN_MODULES[name]], env=env,
                                     cwd=project_root)

    def _tool_name(self, server: str, tool: str) -> str:
        spec = self._spec(server)
        return spec.tools.get(tool, tool) if spec else tool

    # -- sessions -------------------------------------------------------
    async def session(self, name: str) -> ClientSession:
        if name in self._sessions:
            return self._sessions[name]
        if not self.enabled(name):
            raise IntegrationDisabled(f"{name} integration is disabled")
        if name in self._failed:
            raise MCPToolError(f"{name} server unavailable: {self._failed[name]}")
        log_dir = self.cfg.data_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        errlog = self._stack.enter_context(open(log_dir / f"mcp-{name}.log", "a", encoding="utf-8"))
        try:
            read, write = await self._stack.enter_async_context(stdio_client(self._params(name), errlog=errlog))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as exc:  # noqa: BLE001
            self._failed[name] = str(exc) or type(exc).__name__
            raise MCPToolError(f"could not start the {name} MCP server: {self._failed[name]}") from exc
        self._sessions[name] = session
        return session

    async def list_tools(self, name: str) -> list[dict[str, Any]]:
        s = await self.session(name)
        res = await s.list_tools()
        return [{"name": t.name, "description": (t.description or "").strip().splitlines()[0] if t.description else ""}
                for t in res.tools]

    async def call(
        self,
        server: str,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        capability: ExecutorCapability | None = None,
        timeout: float = 180,
    ) -> dict[str, Any]:
        if (server, tool) in EXECUTOR_ONLY and not isinstance(capability, ExecutorCapability):
            raise PermissionError(f"{server}.{tool} acts on the outside world and is reserved for the executor")
        session = await self.session(server)
        res = await session.call_tool(self._tool_name(server, tool), args or {}, read_timeout_seconds=timeout)
        texts = [getattr(c, "text", "") for c in getattr(res, "content", []) or []]
        self.calls.append({"server": server, "tool": tool})
        if getattr(res, "is_error", False):
            raise MCPToolError(f"{server}.{tool}: {' '.join(texts)[:500]}")
        structured = getattr(res, "structured_content", None)
        if isinstance(structured, dict) and structured:
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        text = "".join(texts)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"text": text}
        return data if isinstance(data, dict) else {"result": data}
