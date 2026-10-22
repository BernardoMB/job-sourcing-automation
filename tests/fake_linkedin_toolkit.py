"""A stand-in for linkedin-toolkit's MCP server, following its published contract (v2.1.1).

Same tool names, required parameters, and result shapes (threads with participants[{publicId,
fullName}] and lastMessageAt in ms; messages with fromPublicId/body/sentAt in ms; sends to a
publicId that come back queued, as in Copilot mode), plus decoy tools that must not be picked.
Sends are recorded in the JSON file named by FAKE_LI_STATE.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

STATE = Path(os.environ["FAKE_LI_STATE"])
# Toolkit queue: {queue id: {"status": pending|sent|failed|rejected, "error": ...}}; Copilot mode refuses approvals.
QUEUE = STATE.with_name(STATE.stem + "_queue.json")
COPILOT = os.environ.get("FAKE_LI_MODE", "autopilot") == "copilot"
NOW = int(time.time() * 1000)
HOUR = 3_600_000

THREADS = [
    {"threadId": "t-recent", "participants": [{"publicId": "maria-lopez", "fullName": "Maria Lopez"}],
     "lastMessageAt": NOW - HOUR, "unread": True, "snippet": "We're hiring a Senior Platform Engineer..."},
    {"threadId": "t-old", "participants": [{"publicId": "old-contact", "fullName": "Old Contact"}],
     "lastMessageAt": NOW - 30 * 24 * HOUR, "unread": False, "snippet": "Long ago"},
    {"threadId": "t-answered", "participants": [{"publicId": "sam-lee", "fullName": "Sam Lee"}],
     "lastMessageAt": NOW - 2 * HOUR, "unread": False, "snippet": "Thanks!"},
]
MESSAGES = [
    {"messageId": "m1", "threadId": "t-recent", "fromPublicId": "me", "body": "Hi Maria, thanks for connecting!",
     "sentAt": NOW - 3 * HOUR},
    {"messageId": "m2", "threadId": "t-recent", "fromPublicId": "maria-lopez",
     "body": "Hi Alex! We're hiring a Senior Platform Engineer at Acme Cloud. Do you have experience with Kubernetes?",
     "sentAt": NOW - HOUR},
    {"messageId": "m3", "threadId": "t-old", "fromPublicId": "old-contact", "body": "Are you open to roles?",
     "sentAt": NOW - 30 * 24 * HOUR},
    {"messageId": "m4", "threadId": "t-answered", "fromPublicId": "sam-lee", "body": "Open to a chat?",
     "sentAt": NOW - 3 * HOUR},
    {"messageId": "m5", "threadId": "t-answered", "fromPublicId": "me", "body": "Sure, Tuesday works.",
     "sentAt": NOW - 2 * HOUR},
]

app = MCPServer("linkedin-toolkit-fake", log_level="WARNING")


@app.tool()
def linkedin_get_conversations(since: float = 0, unreadOnly: bool = False, count: int = 20) -> dict:
    """List LinkedIn inbox threads, optionally only those since a timestamp or only unread."""
    threads = [t for t in THREADS if t["lastMessageAt"] >= since and (t["unread"] or not unreadOnly)]
    return {"threads": threads[:count]}


@app.tool()
def linkedin_get_messages(threadId: str, since: float = 0) -> dict:
    """Fetch the messages in one thread by threadId."""
    return {"messages": [m for m in MESSAGES if m["threadId"] == threadId and m["sentAt"] >= since]}


@app.tool()
def linkedin_send_message(publicId: str, body: str, dry_run: bool = False) -> dict:
    """Send a direct message to a first-degree connection. In Copilot mode it is queued for approval."""
    sent = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else []
    sent.append({"publicId": publicId, "body": body})
    STATE.write_text(json.dumps(sent), encoding="utf-8")
    queue = _queue()
    queue[f"q_{len(sent)}"] = {"status": "pending"}
    QUEUE.write_text(json.dumps(queue), encoding="utf-8")
    return {"status": "queued", "queueId": f"q_{len(sent)}"}


def _queue() -> dict:
    return json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {}


def _decide(ids: list[str], status: str) -> dict:
    if COPILOT:
        raise RuntimeError(json.dumps({"error": {"code": "UNAUTHORIZED", "message": "In Copilot mode approval is a "
                                                                                     "human action in the popup."}}))
    queue = _queue()
    for i in ids:
        queue.setdefault(i, {})["status"] = status
    QUEUE.write_text(json.dumps(queue), encoding="utf-8")
    return {"updated": ids}


@app.tool()
def linkedin_queue_approve(ids: list[str], dry_run: bool = False) -> dict:
    """Approve queued writes by id so the extension sends them (Autopilot only)."""
    return _decide(ids, "sent")


@app.tool()
def linkedin_queue_reject(ids: list[str], dry_run: bool = False) -> dict:
    """Reject queued writes by id so they are never sent (Autopilot only)."""
    return _decide(ids, "rejected")


@app.tool()
def linkedin_send_inmail(publicId: str, subject: str, body: str, dry_run: bool = False) -> dict:
    """Send an InMail (decoy: must never be used for replies)."""
    raise RuntimeError("decoy tool called")


@app.tool()
def linkedin_send_invite(publicId: str, note: str = "", dry_run: bool = False) -> dict:
    """Send a connection invite (decoy)."""
    raise RuntimeError("decoy tool called")


@app.tool()
def linkedin_queue_list(status: str = "") -> dict:
    """List the human-approval queue, optionally filtered by status."""
    items = [{"id": k, "action": "outreach.message", "status": v.get("status", "pending"),
              **({"result": {"error": {"code": v.get("code", ""), "message": v["error"]}}} if v.get("error") else {})}
             for k, v in _queue().items()]
    return {"items": [i for i in items if not status or i["status"] == status]}


def serve_http(port: int) -> None:
    """Like `lit serve --http`: MCP at /mcp on loopback, bearer token required."""
    import uvicorn
    from starlette.responses import JSONResponse

    inner = app.streamable_http_app()
    token = os.environ.get("FAKE_LI_TOKEN", "")

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if auth != f"Bearer {token}":
                await JSONResponse({"error": {"code": "UNAUTHORIZED"}}, status_code=401)(scope, receive, send)
                return
        await inner(scope, receive, send)

    uvicorn.run(guarded, host="127.0.0.1", port=port, log_level="warning", lifespan="on")


if __name__ == "__main__":
    import sys

    if "--http" in sys.argv:
        serve_http(int(sys.argv[sys.argv.index("--http") + 1]))
    else:
        app.run()
