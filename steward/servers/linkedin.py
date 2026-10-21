"""LinkedIn MCP server: reads and replies through YOUR logged-in browser session.

Steward never scrapes LinkedIn and never runs a headless browser. In
`upstream` mode this server is a thin, rate-limited proxy in front of a LinkedIn
MCP server that drives your own Chrome session (for example the open-source
linkedin-toolkit Chrome extension + MCP server). Pacing is non-negotiable and
enforced here: hourly and daily caps, business hours, and a jittered gap
between sends. Sends that are not allowed yet come back as `deferred`.
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from steward.linkedin_adapt import UPSTREAM_RULES, adapt_args, pick_upstream_tool
from steward.servers.common import READ_ONLY_REMOTE, WRITE, JsonStore, guarded, make_server, server_config
from steward.timeutil import now_iso, parse_iso, to_iso, tz, utcnow

cfg = server_config()
li_cfg = cfg.linkedin
mcp = make_server(
    "steward-linkedin",
    "Read LinkedIn conversations and (executor only) send human-paced replies through the user's own session.",
)
pacing = JsonStore(cfg.data_path / "linkedin_pacing.json", {"sends": [], "next_allowed_at": ""})


# -- pacing -----------------------------------------------------------------
def _pacing_check() -> dict[str, Any]:
    state = pacing.load()
    now = utcnow()
    sends = [parse_iso(s) for s in state.get("sends", []) if parse_iso(s)]
    sends = [s for s in sends if s > now - timedelta(days=1)]
    last_hour = [s for s in sends if s > now - timedelta(hours=1)]
    local = now.astimezone(tz(cfg.profile.timezone))
    start_s, end_s = li_cfg.business_hours.split("-")
    open_t = local.replace(hour=int(start_s[:2]), minute=int(start_s[3:5]), second=0, microsecond=0)
    close_t = local.replace(hour=int(end_s[:2]), minute=int(end_s[3:5]), second=0, microsecond=0)
    reason, retry = "", 0
    next_allowed = parse_iso(state.get("next_allowed_at"))
    if len(sends) >= li_cfg.daily_cap:
        reason, retry = "daily cap reached", int((min(sends) + timedelta(days=1) - now).total_seconds()) + 60
    elif len(last_hour) >= li_cfg.hourly_cap:
        reason, retry = "hourly cap reached", int((min(last_hour) + timedelta(hours=1) - now).total_seconds()) + 60
    elif not (open_t <= local < close_t):
        target = open_t if local < open_t else open_t + timedelta(days=1)
        reason, retry = "outside business hours", int((target - local).total_seconds()) + 60
    elif next_allowed and now < next_allowed:
        reason, retry = "pacing gap between sends", int((next_allowed - now).total_seconds()) + 1
    return {"allowed": not reason, "reason": reason, "retry_after_seconds": max(retry, 0),
            "sent_last_hour": len(last_hour), "sent_last_day": len(sends),
            "hourly_cap": li_cfg.hourly_cap, "daily_cap": li_cfg.daily_cap}


def _record_send() -> None:
    state = pacing.load()
    now = utcnow()
    sends = [s for s in state.get("sends", []) if (parse_iso(s) or now) > now - timedelta(days=1)]
    sends.append(to_iso(now))
    gap = random.randint(li_cfg.min_delay_seconds, max(li_cfg.min_delay_seconds, li_cfg.max_delay_seconds))
    pacing.save({"sends": sends, "next_allowed_at": to_iso(now + timedelta(seconds=gap))})


# -- sandbox backend ----------------------------------------------------------
class SandboxLinkedIn:
    def __init__(self) -> None:
        self.store = JsonStore(cfg.sandbox_path / "linkedin.json", {"conversations": [], "sent": []})

    async def list(self, since: datetime | None, limit: int) -> list[dict[str, Any]]:
        out = []
        for c in self.store.load()["conversations"]:
            msgs = c.get("messages", [])
            inbound = [m for m in msgs if m.get("from") == "them"]
            if not inbound:
                continue
            last = parse_iso(inbound[-1]["sent_at"])
            if since and last and last <= since:
                continue
            out.append(self._norm(c))
        return out[:limit]

    async def get(self, conversation_id: str, limit: int) -> dict[str, Any]:
        for c in self.store.load()["conversations"]:
            if c["id"] == conversation_id:
                conv = self._norm(c)
                conv["messages"] = conv["messages"][-limit:]
                return conv
        raise ValueError(f"No conversation {conversation_id}")

    async def send(self, conversation_id: str, text: str) -> dict[str, Any]:
        data = self.store.load()
        conv = next((c for c in data["conversations"] if c["id"] == conversation_id), None)
        if conv is None:
            raise ValueError(f"No conversation {conversation_id}")
        msg = {"id": f"m-{uuid.uuid4().hex[:8]}", "from": "me", "text": text, "sent_at": now_iso()}
        conv.setdefault("messages", []).append(msg)
        data.setdefault("sent", []).append({"conversation_id": conversation_id, **msg})
        self.store.save(data)
        return {"status": "sent", "message_id": msg["id"]}

    @staticmethod
    def _norm(c: dict[str, Any]) -> dict[str, Any]:
        p = c.get("participant", {})
        return {
            "id": c["id"],
            "participant_name": p.get("name", ""),
            "participant_headline": p.get("headline", ""),
            "profile_url": p.get("profile_url", ""),
            "messages": [
                {"id": m["id"], "from": m["from"], "text": m["text"], "sent_at": m["sent_at"]}
                for m in c.get("messages", [])
            ],
        }


# -- upstream proxy -------------------------------------------------------------
def _pick(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


class UpstreamLinkedIn:
    """Proxy to an upstream LinkedIn MCP server that operates your own browser session."""

    def __init__(self) -> None:
        if li_cfg.upstream is None:
            raise RuntimeError("linkedin.backend is 'upstream' but linkedin.upstream is not configured")
        self.spec = li_cfg.upstream
        self.catalog = JsonStore(cfg.data_path / "linkedin_upstream_tools.json", {"tools": {}})

    async def _session_call(self, tool: str | None, args: dict[str, Any] | None, refresh: bool = False) -> Any:
        """Open the upstream server, refresh the tool catalog if needed, and optionally call one tool."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        env = {**get_default_environment(), **self.spec.env}
        params = StdioServerParameters(command=self.spec.command, args=self.spec.args, env=env)
        res = None
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                if refresh or not self.catalog.load()["tools"]:
                    listed = await session.list_tools()
                    self.catalog.save({"tools": {t.name: {"description": t.description or "",
                                                          "schema": t.input_schema if hasattr(t, "input_schema") else
                                                          getattr(t, "inputSchema", {})}
                                                 for t in listed.tools}})
                if tool:
                    res = await session.call_tool(tool, args or {})
        return res

    async def discover(self, refresh: bool = False) -> dict[str, Any]:
        await self._session_call(None, None, refresh=refresh)
        tools = self.catalog.load()["tools"]
        return {"tools": sorted(tools), "mapping": {k: pick_upstream_tool(k, tools, self.spec.tools)
                                                     for k in UPSTREAM_RULES}}

    async def _call(self, logical: str, args: dict[str, Any]) -> Any:
        if not self.catalog.load()["tools"]:
            await self._session_call(None, None, refresh=True)
        tools = self.catalog.load()["tools"]
        name = pick_upstream_tool(logical, tools, self.spec.tools)
        if not name:
            raise RuntimeError(
                f"Could not tell which upstream tool does '{logical}'. Upstream tools: {', '.join(sorted(tools))}. "
                f"Set linkedin.upstream.tools.{logical} in steward.yaml.")
        res = await self._session_call(name, adapt_args((tools.get(name) or {}).get("schema"), args))
        if getattr(res, "is_error", False):
            text = " ".join(getattr(c, "text", "") for c in res.content)
            raise RuntimeError(f"upstream {logical} failed: {text[:300]}")
        if getattr(res, "structured_content", None):
            return res.structured_content
        text = "".join(getattr(c, "text", "") for c in res.content)
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    @staticmethod
    def _norm_messages(raw: Any) -> list[dict[str, Any]]:
        items = raw if isinstance(raw, list) else (raw or {}).get("messages", [])
        out = []
        for m in items or []:
            if not isinstance(m, dict):
                continue
            sender = str(_pick(m, "from", "sender", "author", "senderName", default=""))
            mine = bool(m.get("isMe") or m.get("fromMe") or sender.lower() in ("me", "self", "you"))
            out.append({
                "id": str(_pick(m, "id", "messageId", "urn", default=uuid.uuid4().hex[:8])),
                "from": "me" if mine else "them",
                "text": str(_pick(m, "text", "body", "content", "message", default="")),
                "sent_at": str(_pick(m, "sent_at", "sentAt", "createdAt", "timestamp", "date", default=now_iso())),
            })
        return out

    def _norm_conv(self, c: dict[str, Any]) -> dict[str, Any]:
        participant = c.get("participant") or (c.get("participants") or [{}])[0] or {}
        if isinstance(participant, str):
            participant = {"name": participant}
        return {
            "id": str(_pick(c, "id", "conversationId", "threadId", "urn")),
            "participant_name": str(_pick(participant, "name", "fullName", default=_pick(c, "name", default=""))),
            "participant_headline": str(_pick(participant, "headline", "title", default="")),
            "profile_url": str(_pick(participant, "profile_url", "profileUrl", "url", default="")),
            "messages": self._norm_messages(c.get("messages", [])),
        }

    async def list(self, since: datetime | None, limit: int) -> list[dict[str, Any]]:
        raw = await self._call("list_conversations", {"limit": limit})
        convs = raw if isinstance(raw, list) else raw.get("conversations", raw.get("items", []))
        out = []
        for c in convs or []:
            conv = self._norm_conv(c)
            if not conv["messages"]:
                conv = await self.get(conv["id"], 20)
            inbound = [m for m in conv["messages"] if m["from"] == "them"]
            if not inbound:
                continue
            last = parse_iso(inbound[-1]["sent_at"])
            if since and last and last <= since:
                continue
            out.append(conv)
        return out

    async def get(self, conversation_id: str, limit: int) -> dict[str, Any]:
        raw = await self._call("get_conversation", {"conversation_id": conversation_id, "limit": limit})
        conv = self._norm_conv(raw if isinstance(raw, dict) else {"id": conversation_id, "messages": raw})
        conv["id"] = conv["id"] or conversation_id
        return conv

    async def send(self, conversation_id: str, text: str) -> dict[str, Any]:
        raw = await self._call("send_message", {"conversation_id": conversation_id, "text": text})
        status = str((raw or {}).get("status", "sent")).lower()
        if status == "queued":
            return {"status": "queued", "queue_id": raw.get("queueId", ""), "note": "queued in the LinkedIn toolkit"}
        return {"status": "sent", "message_id": str(_pick(raw or {}, "id", "messageId", default=""))}


_backend: SandboxLinkedIn | UpstreamLinkedIn | None = None


def backend() -> SandboxLinkedIn | UpstreamLinkedIn:
    global _backend
    if _backend is None:
        if li_cfg.backend == "upstream":
            _backend = UpstreamLinkedIn()
        elif li_cfg.backend == "sandbox":
            _backend = SandboxLinkedIn()
        else:
            raise RuntimeError("LinkedIn integration is disabled in steward.yaml")
    return _backend


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def list_conversations(since: str = "", limit: int = 20) -> dict[str, Any]:
    """Conversations with an inbound message newer than `since` (ISO), including recent messages."""
    convs = await backend().list(parse_iso(since) if since else None, limit)
    return {"conversations": convs}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def get_conversation(conversation_id: str, limit: int = 20) -> dict[str, Any]:
    """One conversation with its most recent messages."""
    return await backend().get(conversation_id, limit)


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def pacing_status() -> dict[str, Any]:
    """Whether a send is allowed right now, and current hourly/daily usage."""
    return _pacing_check()


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def upstream_tools(refresh: bool = False) -> dict[str, Any]:
    """The upstream LinkedIn server's tools and which one Steward uses for each operation."""
    b = backend()
    if not isinstance(b, UpstreamLinkedIn):
        return {"backend": li_cfg.backend, "tools": [], "mapping": {}}
    return {"backend": "upstream", **(await b.discover(refresh=refresh))}


@mcp.tool(annotations=WRITE)
@guarded
async def send_message(conversation_id: str, text: str) -> dict[str, Any]:
    """Send a reply at human pace. Returns status sent, queued, or deferred (with retry_after_seconds)."""
    check = _pacing_check()
    if not check["allowed"]:
        return {"status": "deferred", "reason": check["reason"], "retry_after_seconds": check["retry_after_seconds"]}
    result = await backend().send(conversation_id, text)
    _record_send()
    return result


if __name__ == "__main__":
    mcp.run()
