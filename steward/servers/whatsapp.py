"""WhatsApp MCP server: reads and replies to one-to-one chats through your own WhatsApp account.

In `upstream` mode this server is a thin, paced proxy in front of a WhatsApp MCP server that is linked to
your account as a WhatsApp "linked device" (for example whatsapp-mcp-ts). Group chats are never read
unless `whatsapp.include_groups` is on. Which chats are about a job is decided by the orchestrator: every
other chat is ignored completely (nothing stored, nothing logged, nothing answered).

Pacing is the same as LinkedIn's: hourly and daily caps, business hours, and a jittered gap between sends.
Sends that are not allowed yet come back as `deferred`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from steward.servers.common import READ_ONLY_REMOTE, WRITE, JsonStore, Pacer, guarded, make_server, server_config
from steward.timeutil import now_iso, parse_iso, to_iso, utcnow

cfg = server_config()
wa_cfg = cfg.whatsapp
mcp = make_server(
    "steward-whatsapp",
    "Read WhatsApp chats and (executor only) send human-paced replies through the user's own account.",
)
pacer = Pacer(cfg.data_path / "whatsapp_pacing.json", wa_cfg, cfg.profile.timezone)
# The upstream's tool for each operation (whatsapp-mcp-ts names); whatsapp.upstream.tools overrides them.
DEFAULT_TOOLS = {"list_chats": "list_chats", "list_messages": "list_messages", "send_message": "send_message"}


def is_group(chat_id: str) -> bool:
    return chat_id.endswith("@g.us")


def phone_of(chat_id: str) -> str:
    """+15551234567 from 15551234567@s.whatsapp.net ('' for groups and hidden-number ids)."""
    user = chat_id.split("@")[0].split(":")[0]
    return f"+{user}" if chat_id.endswith("@s.whatsapp.net") and user.isdigit() else ""


# -- sandbox backend ----------------------------------------------------------
class SandboxWhatsApp:
    def __init__(self) -> None:
        self.store = JsonStore(cfg.sandbox_path / "whatsapp.json", {"chats": [], "sent": []})

    async def list(self, since: datetime | None, limit: int) -> list[dict[str, Any]]:
        out = []
        for c in self.store.load()["chats"]:
            inbound = [m for m in c.get("messages", []) if m.get("from") == "them"]
            if not inbound or (is_group(c["id"]) and not wa_cfg.include_groups):
                continue
            last = parse_iso(inbound[-1]["sent_at"])
            if since and last and last <= since:
                continue
            out.append(self._norm(c))
        return out[:limit]

    async def get(self, chat_id: str, limit: int) -> dict[str, Any]:
        for c in self.store.load()["chats"]:
            if c["id"] == chat_id:
                conv = self._norm(c)
                conv["messages"] = conv["messages"][-limit:]
                return conv
        raise ValueError(f"No chat {chat_id}")

    async def send(self, chat_id: str, text: str) -> dict[str, Any]:
        data = self.store.load()
        chat = next((c for c in data["chats"] if c["id"] == chat_id), None)
        if chat is None:
            raise ValueError(f"No chat {chat_id}")
        msg = {"id": f"wa-{uuid.uuid4().hex[:8]}", "from": "me", "text": text, "sent_at": now_iso()}
        chat.setdefault("messages", []).append(msg)
        data.setdefault("sent", []).append({"chat_id": chat_id, **msg})
        self.store.save(data)
        return {"status": "sent", "message_id": msg["id"]}

    @staticmethod
    def _norm(c: dict[str, Any]) -> dict[str, Any]:
        msgs = [{"id": m["id"], "from": m["from"], "text": m["text"], "sent_at": m["sent_at"]}
                for m in c.get("messages", [])]
        return {"id": c["id"], "participant_name": c.get("name", ""), "phone": phone_of(c["id"]),
                "is_group": is_group(c["id"]), "last_activity": msgs[-1]["sent_at"] if msgs else "",
                "messages": msgs}


# -- upstream proxy -------------------------------------------------------------
def _decode(res: Any) -> Any:
    """An MCP tool result as data: structured content, JSON text, or the plain text itself."""
    if getattr(res, "structured_content", None):
        return res.structured_content
    text = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(text)
    except ValueError:
        return text


def _items(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = next((data[k] for k in keys if isinstance(data.get(k), list)), [])
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []  # "No chats found." -> []


class UpstreamWhatsApp:
    """Proxy to an upstream WhatsApp MCP server (built against whatsapp-mcp-ts: chats have jid, name, is_group,
    last_message_time; messages have id, content, timestamp, is_from_me; send_message takes recipient and
    message). The upstream holds the WhatsApp connection, so one session serves every call of a check."""

    def __init__(self) -> None:
        if wa_cfg.upstream is None:
            raise RuntimeError("whatsapp.backend is 'upstream' but whatsapp.upstream is not configured")
        self.spec = wa_cfg.upstream
        self.tools = {**DEFAULT_TOOLS, **self.spec.tools}
        self.names = JsonStore(cfg.data_path / "whatsapp_chats.json", {})  # chat id -> name, for replies
        self._requests: asyncio.Queue | None = None
        self._ready: asyncio.Future | None = None

    def _check_linked(self) -> None:
        creds = self._auth_dir() / "creds.json" if self._auth_dir() else None
        if creds is not None and not creds.exists():
            raise RuntimeError("WhatsApp is not linked yet: run `steward whatsapp-link` once and scan the QR code "
                               "with WhatsApp (Settings > Linked devices). Until then the upstream would ask for a "
                               "new QR code on every check.")

    def _auth_dir(self) -> Path | None:
        """whatsapp-mcp-ts keeps its login in auth_info next to src/main.ts."""
        main = next((a for a in self.spec.args if a.replace("\\", "/").endswith("src/main.ts")), "")
        return Path(main).resolve().parent.parent / "auth_info" if main else None

    @contextlib.asynccontextmanager
    async def _transport(self):
        if self.spec.url:
            from mcp.client.streamable_http import streamable_http_client

            async with streamable_http_client(self.spec.url) as streams:
                yield streams[0], streams[1]
            return
        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        main = self._auth_dir()
        params = StdioServerParameters(command=self.spec.command, args=self.spec.args,
                                       env={**get_default_environment(), **self.spec.env},
                                       cwd=str(main.parent) if main else None)
        log = cfg.data_path / "logs" / "whatsapp-upstream.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (r, w):
                yield r, w

    async def _serve(self) -> None:
        """Owns the upstream session for this server's lifetime and runs calls handed to it in order."""
        from mcp import ClientSession

        assert self._requests is not None and self._ready is not None
        try:
            self._check_linked()
            async with self._transport() as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    if not self.spec.url:
                        # A freshly started upstream reconnects to WhatsApp and then receives what arrived
                        # while it was offline.
                        await asyncio.sleep(wa_cfg.settle_seconds)
                    self._ready.set_result(True)
                    while True:
                        name, args, done = await self._requests.get()
                        try:
                            res = await session.call_tool(name, args)
                            if getattr(res, "is_error", False):
                                text = " ".join(getattr(c, "text", "") for c in res.content)
                                raise RuntimeError(f"WhatsApp ({name}): {text[:300]}")
                            done.set_result(_decode(res))
                        except Exception as exc:  # noqa: BLE001 - handed back to the caller
                            done.set_exception(exc)
        except Exception as exc:  # noqa: BLE001
            if not self._ready.done():
                self._ready.set_exception(exc)
            self._requests = None  # the next call starts over

    async def call(self, logical: str, args: dict[str, Any]) -> Any:
        if self._requests is None:
            self._requests = asyncio.Queue()
            self._ready = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().create_task(self._serve())
        await asyncio.shield(self._ready)
        done = asyncio.get_running_loop().create_future()
        await self._requests.put((self.tools[logical], args, done))
        return await asyncio.wait_for(done, timeout=120)

    def _norm_messages(self, raw: Any) -> list[dict[str, Any]]:
        out = []
        for m in _items(raw, "messages", "items"):
            text = str(m.get("content") or m.get("text") or m.get("body") or "")
            if not text:
                continue
            when = parse_iso(str(m.get("timestamp") or m.get("sent_at") or ""))
            out.append({"id": str(m.get("id") or uuid.uuid4().hex[:8]),
                        "from": "me" if m.get("is_from_me") or m.get("from_me") else "them",
                        "text": text, "sent_at": to_iso(when) if when else now_iso()})
        return sorted(out, key=lambda x: x["sent_at"])

    async def _messages(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        return self._norm_messages(await self.call("list_messages", {"chat_jid": chat_id, "limit": limit, "page": 0}))

    async def list(self, since: datetime | None, limit: int) -> list[dict[str, Any]]:
        since = since or (utcnow() - timedelta(days=wa_cfg.initial_lookback_days))
        raw = await self.call("list_chats", {"limit": max(limit, wa_cfg.max_chats), "page": 0,
                                             "sort_by": "last_active", "include_last_message": True})
        known = self.names.load()
        out = []
        for c in _items(raw, "chats", "items"):
            chat_id = str(c.get("jid") or c.get("id") or "")
            group = bool(c.get("is_group")) or is_group(chat_id)
            if not chat_id or chat_id == "status@broadcast" or (group and not wa_cfg.include_groups):
                continue
            last = parse_iso(str(c.get("last_message_time") or ""))
            if last is None or last <= since:
                continue
            if c.get("last_is_from_me"):
                continue  # you wrote last: nothing new from them
            msgs = await self._messages(chat_id, 20)
            inbound = [m for m in msgs if m["from"] == "them"]
            if not inbound or (parse_iso(inbound[-1]["sent_at"]) or utcnow()) <= since:
                continue
            name = str(c.get("name") or "")
            if name and name != chat_id.split("@")[0]:
                known[chat_id] = name
            out.append({"id": chat_id, "participant_name": known.get(chat_id, "") or phone_of(chat_id),
                        "phone": phone_of(chat_id), "is_group": group,
                        "last_activity": to_iso(last), "messages": msgs})
            if len(out) >= limit:
                break
        self.names.save(known)
        return out

    async def get(self, chat_id: str, limit: int) -> dict[str, Any]:
        msgs = await self._messages(chat_id, limit)
        return {"id": chat_id, "participant_name": self.names.load().get(chat_id, "") or phone_of(chat_id),
                "phone": phone_of(chat_id), "is_group": is_group(chat_id),
                "last_activity": msgs[-1]["sent_at"] if msgs else "", "messages": msgs}

    async def send(self, chat_id: str, text: str) -> dict[str, Any]:
        raw = await self.call("send_message", {"recipient": chat_id, "message": text})
        found = re.search(r"\(ID: ([^)]+)\)", raw) if isinstance(raw, str) else None
        ident = found.group(1) if found else str((raw or {}).get("id", "")) if isinstance(raw, dict) else ""
        return {"status": "sent", "message_id": ident}


_backend: SandboxWhatsApp | UpstreamWhatsApp | None = None


def backend() -> SandboxWhatsApp | UpstreamWhatsApp:
    global _backend
    if _backend is None:
        if wa_cfg.backend == "upstream":
            _backend = UpstreamWhatsApp()
        elif wa_cfg.backend == "sandbox":
            _backend = SandboxWhatsApp()
        else:
            raise RuntimeError("WhatsApp integration is disabled in steward.yaml")
    return _backend


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def list_conversations(since: str = "", limit: int = 20) -> dict[str, Any]:
    """One-to-one chats with an inbound message newer than `since` (ISO), including recent messages."""
    return {"conversations": await backend().list(parse_iso(since) if since else None, limit)}


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def get_conversation(conversation_id: str, limit: int = 20) -> dict[str, Any]:
    """One chat with its most recent messages."""
    return await backend().get(conversation_id, limit)


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def pacing_status() -> dict[str, Any]:
    """Whether a send is allowed right now, and current hourly/daily usage."""
    return pacer.check()


@mcp.tool(annotations=READ_ONLY_REMOTE)
@guarded
async def health() -> dict[str, Any]:
    """Is WhatsApp usable: a real read of the most recent chats."""
    convs = await backend().list(utcnow() - timedelta(days=30), 5)
    return {"backend": wa_cfg.backend, "chats": len(convs)}


@mcp.tool(annotations=WRITE)
@guarded
async def send_message(conversation_id: str, text: str, force: bool = False) -> dict[str, Any]:
    """Send a reply at human pace. Returns status sent or deferred (with retry_after_seconds).

    `force` is your explicit "Execute now": it skips business hours, caps, and the gap between sends.
    """
    if is_group(conversation_id):
        raise ValueError("Job Sourcer never writes in WhatsApp groups")
    check = pacer.check()
    if not check["allowed"] and not force:
        return {"status": "deferred", "reason": check["reason"], "retry_after_seconds": check["retry_after_seconds"]}
    result = await backend().send(conversation_id, text)
    pacer.record()
    return result


if __name__ == "__main__":
    mcp.run()
