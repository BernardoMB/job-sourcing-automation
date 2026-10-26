from __future__ import annotations

import functools
import inspect
import json
import os
import random
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from steward.config import Config, load_config
from steward.timeutil import parse_iso, to_iso, tz, utcnow

F = TypeVar("F", bound=Callable[..., Any])

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
READ_ONLY_REMOTE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def server_config() -> Config:
    return load_config(os.environ.get("STEWARD_CONFIG"))


def make_server(name: str, instructions: str) -> MCPServer:
    return MCPServer(name, instructions=instructions, log_level="WARNING")


def _leaf(exc: BaseException) -> BaseException:
    """The real error inside nested ExceptionGroups (async task groups wrap failures in them)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


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
                leaf = _leaf(exc)
                raise ToolError(f"{type(leaf).__name__}: {leaf}") from exc

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


class Pacer:
    """Human pacing for a messaging channel (LinkedIn, WhatsApp): hourly and daily caps, business hours, and a
    jittered gap between sends. `limits` has hourly_cap, daily_cap, business_hours ("09:00-19:00"),
    min_delay_seconds and max_delay_seconds."""

    def __init__(self, path: Path, limits: Any, timezone: str):
        self.store = JsonStore(path, {"sends": [], "next_allowed_at": ""})
        self.limits = limits
        self.timezone = timezone

    def check(self) -> dict[str, Any]:
        lim = self.limits
        state = self.store.load()
        now = utcnow()
        sends = [parse_iso(s) for s in state.get("sends", []) if parse_iso(s)]
        sends = [s for s in sends if s > now - timedelta(days=1)]
        last_hour = [s for s in sends if s > now - timedelta(hours=1)]
        local = now.astimezone(tz(self.timezone))
        start_s, end_s = lim.business_hours.split("-")
        open_t = local.replace(hour=int(start_s[:2]), minute=int(start_s[3:5]), second=0, microsecond=0)
        close_t = local.replace(hour=int(end_s[:2]), minute=int(end_s[3:5]), second=0, microsecond=0)
        reason, retry = "", 0
        next_allowed = parse_iso(state.get("next_allowed_at"))
        if len(sends) >= lim.daily_cap:
            reason, retry = "daily cap reached", int((min(sends) + timedelta(days=1) - now).total_seconds()) + 60
        elif len(last_hour) >= lim.hourly_cap:
            reason, retry = "hourly cap reached", int((min(last_hour) + timedelta(hours=1) - now).total_seconds()) + 60
        elif not (open_t <= local < close_t):
            target = open_t if local < open_t else open_t + timedelta(days=1)
            reason, retry = "outside business hours", int((target - local).total_seconds()) + 60
        elif next_allowed and now < next_allowed:
            reason, retry = "pacing gap between sends", int((next_allowed - now).total_seconds()) + 1
        return {"allowed": not reason, "reason": reason, "retry_after_seconds": max(retry, 0),
                "sent_last_hour": len(last_hour), "sent_last_day": len(sends),
                "hourly_cap": lim.hourly_cap, "daily_cap": lim.daily_cap}

    def record(self) -> None:
        lim = self.limits
        state = self.store.load()
        now = utcnow()
        sends = [s for s in state.get("sends", []) if (parse_iso(s) or now) > now - timedelta(days=1)]
        sends.append(to_iso(now))
        gap = random.randint(lim.min_delay_seconds, max(lim.min_delay_seconds, lim.max_delay_seconds))
        self.store.save({"sends": sends, "next_allowed_at": to_iso(now + timedelta(seconds=gap))})
