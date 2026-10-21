"""LLM providers. The LLM only ever reasons and drafts; it never calls write tools."""

from __future__ import annotations

from steward.config import Config
from steward.llm.base import LLM, LLMError, LLMRefusal, LLMUnavailable


def get_llm(cfg: Config) -> LLM:
    provider = cfg.llm.provider
    if provider == "claude-code":
        from steward.llm.claude_code_provider import ClaudeCodeLLM

        return ClaudeCodeLLM(cfg.llm, cfg.data_path / "claude-code")
    if provider == "anthropic":
        from steward.llm.anthropic_provider import AnthropicLLM

        return AnthropicLLM(cfg.llm)
    if provider == "grok":
        from steward.llm.grok_provider import GrokLLM

        return GrokLLM(cfg.llm)
    if provider == "offline":
        from steward.llm.offline import OfflineLLM

        return OfflineLLM(cfg)
    raise ValueError(f"Unknown LLM provider: {provider}")


__all__ = ["LLM", "LLMError", "LLMRefusal", "LLMUnavailable", "get_llm"]
