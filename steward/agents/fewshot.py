"""Dynamic few-shot examples: your most similar past decisions, injected into the drafting prompt.

If you keep editing drafts to be shorter or warmer, those edits resurface as
examples and the drafter converges on your style without any fine-tuning.
"""

from __future__ import annotations

import re
from typing import Any

from steward.ledger import Ledger

_WORD = re.compile(r"[a-z0-9+#.]{3,}")
_STOP = {"the", "and", "for", "you", "your", "with", "this", "that", "have", "are", "would", "about", "our", "from",
         "thanks", "best", "regards", "hi", "hello"}


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def retrieve(ledger: Ledger, event: dict[str, Any], cls: dict[str, Any], k: int = 3) -> list[dict[str, Any]]:
    query = tokens(f"{event.get('subject', '')} {event.get('body_text', '')}")
    cats = set(cls.get("categories", []))
    scored = []
    for entry in ledger.decisions():
        p = entry["payload"]
        if p.get("decision") not in ("approved", "edited") or not p.get("final_reply"):
            continue
        other = tokens(p.get("message_excerpt", ""))
        union = query | other
        sim = len(query & other) / len(union) if union else 0.0
        sim += 0.3 * (len(cats & set(p.get("categories", []))) / max(len(cats), 1))
        if p.get("channel") == event.get("source"):
            sim += 0.05
        if sim > 0.12:
            scored.append((sim, entry["seq"], p))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    for sim, _seq, p in scored[:k]:
        ex = {
            "recruiter_message": p.get("message_excerpt", "")[:700],
            "categories": p.get("categories", []),
            "reply_the_candidate_sent": p.get("final_reply", "")[:1200],
            "candidate_edited_the_draft": p.get("decision") == "edited",
        }
        if p.get("decision") == "edited":
            ex["original_draft_before_edit"] = p.get("original_reply", "")[:1200]
        out.append(ex)
    return out
