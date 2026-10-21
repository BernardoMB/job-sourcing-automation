"""CV Analyst subagent: read-only, deterministic evidence retrieval through the CV MCP server.

It does not generate answers. For each question it returns exact CV lines and
years computed from role dates, or `answer_found: false`, which forces the
question to you instead of a guess. Availability, salary, location, and work
authorization come from configuration and the calendar, never from the CV.
"""

from __future__ import annotations

from typing import Any

from steward.agents.base import AgentContext
from steward.vocab import canonical


async def _skill(ctx: AgentContext, skill: str, cache: dict[str, Any]) -> dict[str, Any]:
    key = canonical(skill, ctx.cfg.cv.aliases).lower()
    if key not in cache:
        cache[key] = await ctx.hub.call("cv", "skill_experience", {"skill": skill})
    return cache[key]


def _skill_item(question: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": question["text"],
        "type": question["type"],
        "source": "cv",
        "skill": r["skill"],
        "answer_found": bool(r["found"]),
        "years": r.get("years"),
        "years_source": r.get("years_source"),
        "last_used": r.get("last_used"),
        "roles": r.get("roles", [])[:3],
        "evidence": r.get("evidence", [])[:4],
    }


async def analyze(ctx: AgentContext, cls: dict[str, Any]) -> dict[str, Any]:
    p = ctx.cfg.profile
    cache: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    asked: set[str] = set()
    for q in cls.get("questions", []):
        qtype = q["type"]
        entities = q.get("entities") or [t for t in cls.get("technologies", []) if t.lower() in q["text"].lower()]
        if qtype in ("tech_experience", "years_experience") or (qtype == "other" and entities):
            if entities:
                for ent in entities:
                    r = await _skill(ctx, ent, cache)
                    asked.add(r["skill"].lower())
                    items.append(_skill_item(q, r))
                continue
            if qtype == "years_experience":
                prof = await ctx.hub.call("cv", "get_profile", {})
                items.append({"question": q["text"], "type": qtype, "source": "cv", "answer_found": True,
                              "years": prof.get("total_years"), "skill": "total professional experience",
                              "roles": prof.get("recent_roles", []), "evidence": []})
                continue
        if qtype == "salary":
            items.append({"question": q["text"], "type": qtype, "source": "policy", "answer_found": True,
                          "answer": p.salary_policy})
        elif qtype == "availability":
            items.append({"question": q["text"], "type": qtype, "source": "calendar", "answer_found": True})
        elif qtype == "notice_period":
            items.append({"question": q["text"], "type": qtype, "source": "profile",
                          "answer_found": bool(p.notice_period), "answer": p.notice_period})
        elif qtype == "work_authorization":
            items.append({"question": q["text"], "type": qtype, "source": "profile",
                          "answer_found": bool(p.work_authorization), "answer": p.work_authorization})
        elif qtype == "relocation":
            answer = p.relocation + (f" Preferred locations: {', '.join(p.preferred_locations)}."
                                     if p.preferred_locations else "")
            items.append({"question": q["text"], "type": qtype, "source": "profile", "answer_found": bool(p.relocation),
                          "answer": answer})
        elif qtype == "interest":
            items.append({"question": q["text"], "type": qtype, "source": "profile", "answer_found": True,
                          "answer": "Interested in roles like: " + ", ".join(p.target_roles or ["(not configured)"])})
        else:
            query = " ".join(q.get("search_terms") or [q["text"]])
            res = await ctx.hub.call("cv", "search_cv", {"query": query, "limit": 4})
            hits = [h for h in res.get("hits", []) if h.get("score", 0) >= 2]
            items.append({"question": q["text"], "type": qtype, "source": "cv", "answer_found": bool(hits),
                          "evidence": hits[:4]})

    # Technologies mentioned without a direct question: evidence of fit for the reply.
    fit = []
    for tech in cls.get("technologies", []):
        r = await _skill(ctx, tech, cache)
        if r["skill"].lower() in asked:
            continue
        fit.append({"skill": r["skill"], "found": r["found"], "years": r.get("years"),
                    "evidence": r.get("evidence", [])[:2]})
    unanswered = [i["question"] for i in items if not i["answer_found"]]
    return {
        "items": items,
        "fit": fit,
        "all_answered": not unanswered,
        "unanswered": unanswered,
        "cv_questions": sum(1 for i in items if i["source"] == "cv"),
    }
