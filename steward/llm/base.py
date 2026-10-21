from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Protocol


class LLMError(RuntimeError):
    """The model call failed; the event will be retried, then escalated to you."""


class LLMRefusal(LLMError):
    """The model declined; the event is escalated to you rather than guessed at."""


class LLMUnavailable(LLMError):
    """Claude can't be used right now (usage limit, not signed in, offline).

    Nothing is lost: messages stay queued and are processed once `retry_at` passes.
    """

    def __init__(self, message: str, retry_at: datetime | None = None):
        super().__init__(message)
        self.retry_at = retry_at


class LLM(Protocol):
    name: str

    def model_for(self, purpose: str) -> str: ...

    def json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return an object that satisfies `schema`.

        `purpose` is one of classify, draft, critique, distill. `context` carries the
        structured inputs; real providers ignore it, the offline provider uses it.
        """
        ...


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    raise LLMError("Model did not return valid JSON")
