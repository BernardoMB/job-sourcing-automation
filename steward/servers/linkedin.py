"""LinkedIn MCP server: reads and replies through YOUR logged-in browser session.

Job Sourcer never scrapes LinkedIn and never runs a headless browser. In
`upstream` mode this server is a thin, rate-limited proxy in front of a LinkedIn
MCP server that drives your own Chrome session (for example the open-source
linkedin-toolkit Chrome extension + MCP server). Pacing is non-negotiable and
enforced here: hourly and daily caps, business hours, and a jittered gap
between sends. Sends that are not allowed yet come back as `deferred`.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
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
INITIAL_LOOKBACK_DAYS = 7  # on the very first LinkedIn check, only threads active this past week


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


def _when(value: Any) -> str:
    """ISO 8601 from an ISO string or an epoch timestamp (the toolkit uses milliseconds)."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        return to_iso(datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc))
    d = parse_iso(str(value)) if value else None
    return to_iso(d) if d else now_iso()


def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _toolkit_error(logical: str, tool: str, text: str) -> str:
    """Turn the toolkit's {"error": {"code", "message", "howToFix"}} into one readable sentence."""
    try:
        err = (json.loads(text) or {}).get("error") or {}
    except ValueError:
        err = {}
    if err.get("code") == "EXTENSION_OFFLINE":
        return ("The LinkedIn Toolkit extension isn't connected to its bridge. Make sure Microsoft Edge (or the "
                "browser where you installed the extension) is open and the extension badge says 'connected'.")
    if err.get("message"):
        return f"LinkedIn toolkit ({tool}): {err['message']} {err.get('howToFix', '')}".strip()
    return f"upstream {logical} ({tool}) failed: {text[:300]}"


def _own_public_id() -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", cfg.profile.linkedin_url or "")
    return m.group(1).lower() if m else ""


class UpstreamLinkedIn:
    """Proxy to an upstream LinkedIn MCP server that operates your own browser session.

    Built against linkedin-toolkit's contract (threads: threadId, participants[{publicId, fullName}],
    lastMessageAt in ms; messages: messageId, fromPublicId, body, sentAt in ms; sends go to a
    person's publicId and return {status: sent|queued, queueId}) and tolerant of similar servers.
    """

    def __init__(self) -> None:
        if li_cfg.upstream is None:
            raise RuntimeError("linkedin.backend is 'upstream' but linkedin.upstream is not configured")
        self.spec = li_cfg.upstream
        self.catalog = JsonStore(cfg.data_path / "linkedin_upstream_tools.json", {"tools": {}})
        # threadId -> the other person's publicId and name, so a reply can be addressed to them.
        self.threads = JsonStore(cfg.data_path / "linkedin_threads.json", {})
        self.me = _own_public_id()

    @contextlib.asynccontextmanager
    async def _transport(self):
        """HTTP to a long-running bridge when `url` is set (the toolkit's `lit serve --http`), else stdio."""
        if self.spec.url:
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

            token = os.environ.get(self.spec.token_env, "") if self.spec.token_env else ""
            if self.spec.token_env and not token:
                raise RuntimeError("Paste the LinkedIn toolkit's pairing token in Setup (step 6). Get it with: "
                                   "npx -y -p linkedin-toolkit-mcp lit config get token --reveal")
            import httpx

            try:  # a plain request first, so a refused token gets a plain explanation
                async with httpx.AsyncClient(timeout=10) as probe:
                    pre = await probe.post(self.spec.url, json={},
                                           headers={"Authorization": f"Bearer {token}"} if token else {})
            except httpx.HTTPError as exc:
                raise RuntimeError(f"The LinkedIn toolkit bridge at {self.spec.url} is not reachable ({exc}). "
                                   "Job Sourcer starts it automatically; see data/logs/linkedin-bridge.log.") from exc
            if pre.status_code in (401, 403):
                raise RuntimeError("The LinkedIn toolkit bridge refused the pairing token (HTTP "
                                   f"{pre.status_code}). Paste the current token in Setup (step 6).")
            try:
                async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"} if token else None) \
                        as client:
                    async with streamable_http_client(self.spec.url, http_client=client) as streams:
                        yield streams[0], streams[1]
            except OSError as exc:
                raise RuntimeError(f"The LinkedIn toolkit bridge at {self.spec.url} is not reachable ({exc}). "
                                   "Job Sourcer starts it automatically; see data/logs/linkedin-bridge.log.") from exc
            return
        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        env = {**get_default_environment(), **self.spec.env}
        params = StdioServerParameters(command=self.spec.command, args=self.spec.args, env=env)
        async with stdio_client(params) as (r, w):
            yield r, w

    @contextlib.asynccontextmanager
    async def _open(self, refresh: bool = False):
        """One upstream session for a whole batch of calls."""
        from mcp import ClientSession

        async with self._transport() as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                if refresh or not self.catalog.load()["tools"]:
                    listed = await session.list_tools()
                    self.catalog.save({"tools": {t.name: {"description": t.description or "",
                                                          "schema": t.input_schema if hasattr(t, "input_schema") else
                                                          getattr(t, "inputSchema", {})}
                                                 for t in listed.tools}})
                tools = self.catalog.load()["tools"]

                async def call(logical: str, args: dict[str, Any]) -> Any:
                    name = pick_upstream_tool(logical, tools, self.spec.tools)
                    if not name:
                        raise RuntimeError(
                            f"Could not tell which upstream tool does '{logical}'. Upstream tools: "
                            f"{', '.join(sorted(tools))}. Set linkedin.upstream.tools.{logical} in steward.yaml.")
                    res = await session.call_tool(name, adapt_args((tools.get(name) or {}).get("schema"), args))
                    if getattr(res, "is_error", False):
                        text = " ".join(getattr(c, "text", "") for c in res.content)
                        raise RuntimeError(_toolkit_error(logical, name, text))
                    if getattr(res, "structured_content", None):
                        return res.structured_content
                    text = "".join(getattr(c, "text", "") for c in res.content)
                    try:
                        return json.loads(text)
                    except ValueError:
                        return {"text": text}

                yield call

    async def discover(self, refresh: bool = False) -> dict[str, Any]:
        async with self._open(refresh=refresh):
            pass
        tools = self.catalog.load()["tools"]
        return {"tools": sorted(tools), "mapping": {k: pick_upstream_tool(k, tools, self.spec.tools)
                                                     for k in UPSTREAM_RULES}}

    def _is_mine(self, m: dict[str, Any], others: set[str]) -> bool:
        sender = str(_pick(m, "fromPublicId", "from", "sender", "author", "senderName", default="")).lower()
        if m.get("isMe") or m.get("fromMe") or sender in ("me", "self", "you") or (self.me and sender == self.me):
            return True
        return bool(others) and bool(sender) and sender not in others

    def _norm_messages(self, raw: Any, others: set[str]) -> list[dict[str, Any]]:
        items = raw if isinstance(raw, list) else (raw or {}).get("messages", [])
        out = []
        for m in items or []:
            if not isinstance(m, dict):
                continue
            out.append({
                "id": str(_pick(m, "id", "messageId", "urn", default=uuid.uuid4().hex[:8])),
                "from": "me" if self._is_mine(m, others) else "them",
                "text": str(_pick(m, "text", "body", "content", "message", default="")),
                "sent_at": _when(_pick(m, "sent_at", "sentAt", "createdAt", "timestamp", "date", default="")),
            })
        return sorted(out, key=lambda x: x["sent_at"])

    def _participant(self, c: dict[str, Any]) -> dict[str, Any]:
        people = c.get("participants") or ([c["participant"]] if c.get("participant") else [])
        people = [{"name": p} if isinstance(p, str) else p for p in people if p]
        other = next((p for p in people if str(_pick(p, "publicId", "public_id", default="")).lower() != self.me),
                     people[0] if people else {})
        return other

    def _norm_conv(self, c: dict[str, Any], messages: Any = None) -> dict[str, Any]:
        p = self._participant(c)
        public_id = str(_pick(p, "publicId", "public_id", default=""))
        others = {str(_pick(x, "publicId", "public_id", default="")).lower()
                  for x in (c.get("participants") or []) if isinstance(x, dict)} - {"", self.me}
        url = str(_pick(p, "profile_url", "profileUrl", "url", default=""))
        if not url and public_id:
            url = f"https://www.linkedin.com/in/{public_id}/"
        return {
            "id": str(_pick(c, "threadId", "id", "conversationId", "urn")),
            "participant_name": str(_pick(p, "fullName", "name", default=_pick(c, "name", default=""))),
            "participant_headline": str(_pick(p, "headline", "title", default="")),
            "profile_url": url,
            "public_id": public_id,
            "last_activity": _when(_pick(c, "lastMessageAt", "updatedAt", "lastActivityAt", default="")),
            "messages": self._norm_messages(messages if messages is not None else c.get("messages", []), others),
        }

    def _remember(self, conv: dict[str, Any]) -> None:
        if conv["id"] and conv["public_id"]:
            known = self.threads.load()
            known[conv["id"]] = {"public_id": conv["public_id"], "name": conv["participant_name"]}
            self.threads.save(known)

    async def list(self, since: datetime | None, limit: int) -> list[dict[str, Any]]:
        # First check: only the last week, so years of old threads don't flood the briefing.
        since = since or (utcnow() - timedelta(days=INITIAL_LOOKBACK_DAYS))
        out = []
        async with self._open() as call:
            raw = await call("list_conversations", {"since": _ms(since), "limit": limit})
            threads = raw if isinstance(raw, list) else (raw.get("threads") or raw.get("conversations")
                                                         or raw.get("items") or [])
            for t in threads:
                if not isinstance(t, dict):
                    continue
                conv = self._norm_conv(t)
                if (parse_iso(conv["last_activity"]) or utcnow()) <= since:
                    continue
                if not conv["messages"]:
                    msgs = await call("get_conversation", {"conversation_id": conv["id"], "limit": 20})
                    conv = self._norm_conv(t, msgs)
                conv["messages"] = conv["messages"][-20:]
                self._remember(conv)
                inbound = [m for m in conv["messages"] if m["from"] == "them"]
                if inbound and (parse_iso(inbound[-1]["sent_at"]) or utcnow()) > since:
                    out.append(conv)
        return out

    async def get(self, conversation_id: str, limit: int) -> dict[str, Any]:
        known = self.threads.load().get(conversation_id, {})
        stub = {"threadId": conversation_id,
                "participants": [{"publicId": known.get("public_id", ""), "fullName": known.get("name", "")}]}
        async with self._open() as call:
            msgs = await call("get_conversation", {"conversation_id": conversation_id, "limit": limit})
        conv = self._norm_conv(stub, msgs)
        conv["messages"] = conv["messages"][-limit:]
        return conv

    async def send(self, conversation_id: str, text: str) -> dict[str, Any]:
        async with self._open() as call:
            public_id = self.threads.load().get(conversation_id, {}).get("public_id", "")
            if not public_id:  # not seen in this process's cache yet: look the thread up
                raw = await call("list_conversations", {"limit": 100})
                for t in (raw.get("threads") or raw.get("conversations") or []) if isinstance(raw, dict) else []:
                    conv = self._norm_conv(t)
                    self._remember(conv)
                    if conv["id"] == conversation_id:
                        public_id = conv["public_id"]
            if not public_id:
                raise RuntimeError(f"Could not find who to message in LinkedIn thread {conversation_id}")
            raw = await call("send_message", {"recipient_public_id": public_id, "conversation_id": conversation_id,
                                              "text": text})
        status = str((raw or {}).get("status", "sent")).lower()
        if status == "queued":
            return {"status": "queued", "queue_id": raw.get("queueId", ""), "note": "queued in the LinkedIn toolkit"}
        return {"status": "sent", "message_id": str(_pick(raw or {}, "id", "messageId", "queueId", default=""))}


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
    """The upstream LinkedIn server's tools and which one Job Sourcer uses for each operation."""
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
