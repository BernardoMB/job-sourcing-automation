from __future__ import annotations

import functools
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from steward.config import Config, load_config

F = TypeVar("F", bound=Callable[..., Any])

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
READ_ONLY_REMOTE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def server_config() -> Config:
    return load_config(os.environ.get("STEWARD_CONFIG"))


def make_server(name: str, instructions: str) -> MCPServer:
    return MCPServer(name, instructions=instructions, log_level="WARNING")


def guarded(fn: F) -> F:
    """Surface tool failures to the client as readable errors instead of opaque crashes."""

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure must reach the orchestrator
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper  # type: ignore[return-value]


class JsonStore:
    """Tiny JSON file store used by sandbox backends and pacing state."""

    def __init__(self, path: Path, default: Any):
        self.path = path
        self.default = default

    def load(self) -> Any:
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
