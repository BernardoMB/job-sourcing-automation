from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from steward.config import Config
from steward.db import Database
from steward.ledger import Ledger
from steward.llm import LLM
from steward.mcp_hub import MCPHub


@dataclass
class AgentContext:
    cfg: Config
    db: Database
    llm: LLM
    hub: MCPHub
    ledger: Ledger


def block(tag: str, content: Any) -> str:
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=1, default=str)
    return f"<{tag}>\n{content}\n</{tag}>"


def clamp(value: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


async def call_llm(
    ctx: AgentContext,
    purpose: str,
    subagent: str,
    event_id: int | None,
    system: str,
    user: str,
    schema: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    model = ctx.llm.model_for(purpose)
    try:
        out = await asyncio.to_thread(ctx.llm.json, purpose, system, user, schema, context)
    except Exception as exc:
        ctx.db.log_subagent(subagent, event_id, {"prompt": user[:20000]}, None,
                            int((time.perf_counter() - started) * 1000), model, str(exc))
        raise
    ctx.db.log_subagent(subagent, event_id, {"prompt": user[:20000]}, out,
                        int((time.perf_counter() - started) * 1000), model)
    return out
