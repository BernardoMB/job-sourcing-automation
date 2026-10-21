"""Claude via the official Anthropic SDK, with structured outputs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic

from steward.config import LLMConfig
from steward.llm.base import LLMError, LLMRefusal, LLMUnavailable

# Models that accept the server-side refusal fallback ("fallbacks": "default").
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Models that do not accept output_config.effort.
_NO_EFFORT_PREFIXES = ("claude-haiku-4", "claude-sonnet-4-5", "claude-3")


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
        self.client = anthropic.Anthropic()

    def model_for(self, purpose: str) -> str:
        return self.cfg.models.get(purpose, self.cfg.model)

    def json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model = self.model_for(purpose)
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        effort = self.cfg.effort.get(purpose)
        if effort and not model.startswith(_NO_EFFORT_PREFIXES):
            output_config["effort"] = effort
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": self.cfg.max_tokens,
            # The system prompt is stable across calls; cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        try:
            if self.cfg.refusal_fallback and model in _FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    **request,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Claude authentication failed: set ANTHROPIC_API_KEY or run `ant auth login`.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Claude rejected the request: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Unknown Claude model '{model}'") from exc
        except anthropic.RateLimitError as exc:
            wait = int(exc.response.headers.get("retry-after", "60") or 60)
            raise LLMUnavailable("Claude rate limit reached; new messages wait until it clears.",
                                 datetime.now(timezone.utc) + timedelta(seconds=wait)) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Claude API (network).") from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"Claude declined this {purpose} request (category: {category or 'unspecified'})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude ran out of tokens on the {purpose} request")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise LLMError("Claude returned invalid JSON") from exc
