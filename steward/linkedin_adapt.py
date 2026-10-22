"""Adapting to an upstream LinkedIn MCP server whose tool and argument names are not standardised."""

from __future__ import annotations

from typing import Any

# How to recognise each logical operation among an upstream server's tools (its names are not
# standardised): words that must appear, words that help, words that disqualify.
UPSTREAM_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "list_conversations": (("conversation", "inbox", "thread", "message", "chat"),
                           ("list", "recent", "inbox", "fetch", "get", "unread"),
                           ("send", "reply", "delete", "archive", "mark", "search_people", "profile")),
    "get_conversation": (("conversation", "thread", "message", "chat"),
                         ("get", "read", "history", "fetch", "messages"),
                         ("list", "send", "reply", "delete", "archive", "inbox")),
    "send_message": (("send", "reply"),
                     ("message", "reply", "dm"),
                     ("list", "get", "read", "connection", "invite", "inmail", "post", "comment")),
    # The toolkit's own approval queue (Copilot mode holds every send there).
    "queue_list": (("queue",), ("list",), ("approve", "reject")),
    "queue_approve": (("queue",), ("approve",), ("list", "reject")),
    "queue_reject": (("queue",), ("reject",), ("list", "approve")),
    # Extension connected, logged in, Copilot/Autopilot, business hours, quotas.
    "status": (("status",), ("get",), ("connection", "queue", "campaign")),
}
# When a tool's input schema is known: listing the inbox needs no arguments, while reading one
# thread or sending a message does. (This is what tells "get_conversations" from "get_messages".)
NEEDS_ARGUMENTS = {"list_conversations": False, "get_conversation": True, "send_message": True,
                   "queue_list": False, "queue_approve": True, "queue_reject": True, "status": False}

# Job Sourcer's argument -> (exact names an upstream tool might use, root words to look for otherwise).
ARG_SYNONYMS = {
    "conversation_id": (("conversation_id", "conversationId", "thread_id", "threadId", "conversationUrn", "chat_id",
                         "id", "urn"), ("conversation", "thread", "chat")),
    "recipient_public_id": (("publicId", "public_id", "recipientPublicId", "recipient", "profileId", "memberId"),
                            ("publicid", "recipient", "profileid")),
    "text": (("text", "message", "body", "content", "messageText"), ("text", "message", "body", "content")),
    "limit": (("limit", "count", "max", "maxResults", "pageSize", "page_size"), ("limit", "count", "max", "size")),
    "since": (("since", "sinceMs", "after", "updatedAfter"), ("since", "after")),
}


def _required(spec: Any) -> list[str] | None:
    """The tool's required arguments, or None when Job Sourcer doesn't know its schema."""
    schema = spec.get("schema") if isinstance(spec, dict) else None
    if not isinstance(schema, dict) or not schema:
        return None
    return list(schema.get("required") or [])


def pick_upstream_tool(logical: str, tools: dict[str, Any], configured: dict[str, str]) -> str:
    if configured.get(logical):
        return configured[logical]
    must, helps, avoid = UPSTREAM_RULES[logical]
    best, best_score = "", 0
    for name in tools:
        low = name.lower()
        if not any(w in low for w in must) or any(w in low for w in avoid):
            continue
        required = _required(tools[name])
        if required is not None and bool(required) != NEEDS_ARGUMENTS[logical]:
            continue
        score = 2 * sum(w in low for w in must) + sum(w in low for w in helps)
        if score > best_score or (score == best_score and best and len(name) < len(best)):
            best, best_score = name, score
    return best


def adapt_args(schema: dict[str, Any] | None, wanted: dict[str, Any]) -> dict[str, Any]:
    """Rename Job Sourcer's arguments to whatever the upstream tool's input schema calls them."""
    props = list(((schema or {}).get("properties") or {}).keys())
    if not props:
        return wanted
    out: dict[str, Any] = {}
    for key, value in wanted.items():
        exact, roots = ARG_SYNONYMS.get(key, ((key,), (key,)))
        match = next((p for p in props if p in exact), None) or \
            next((p for p in props if any(r in p.lower() for r in roots)), None)
        if match and match not in out:
            out[match] = value
    return out
