"""Grok (xAI) via its REST chat-completions API with JSON-schema responses."""

from __future__ import annotations

import os
from typing import Any

import httpx

from steward.config import LLMConfig
from steward.llm.base import LLMError, extract_json

XAI_URL = "https://api.x.ai/v1/chat/completions"


class GrokLLM:
    name = "grok"

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.api_key = os.environ.get(cfg.grok_api_key_env, "")
        if not self.api_key:
            raise LLMError(f"Set {cfg.grok_api_key_env} to use Grok.")

    def model_for(self, purpose: str) -> str:
        return self.cfg.models.get(purpose, self.cfg.grok_model)

    def json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "model": self.model_for(purpose),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": purpose, "schema": schema, "strict": True},
            },
        }
        try:
            resp = httpx.post(
                XAI_URL,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=180,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the Grok API: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"Grok API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError("Unexpected Grok response shape") from exc
        return extract_json(content)
