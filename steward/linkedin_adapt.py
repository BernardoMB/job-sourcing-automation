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
}
# Steward's argument -> (exact names an upstream tool might use, root words to look for otherwise).
ARG_SYNONYMS = {
    "conversation_id": (("conversation_id", "conversationId", "thread_id", "threadId", "conversationUrn", "chat_id",
                         "id", "urn"), ("conversation", "thread", "chat")),
    "text": (("text", "message", "body", "content", "messageText"), ("text", "message", "body", "content")),
    "limit": (("limit", "count", "max", "maxResults", "pageSize", "page_size"), ("limit", "count", "max", "size")),
}


def pick_upstream_tool(logical: str, tools: dict[str, Any], configured: dict[str, str]) -> str:
    if configured.get(logical):
        return configured[logical]
    must, helps, avoid = UPSTREAM_RULES[logical]
    best, best_score = "", 0
    for name in tools:
        low = name.lower()
        if not any(w in low for w in must) or any(w in low for w in avoid):
            continue
        score = 2 * sum(w in low for w in must) + sum(w in low for w in helps)
        if score > best_score or (score == best_score and best and len(name) < len(best)):
            best, best_score = name, score
    return best


def adapt_args(schema: dict[str, Any] | None, wanted: dict[str, Any]) -> dict[str, Any]:
    """Rename Steward's arguments to whatever the upstream tool's input schema calls them."""
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
