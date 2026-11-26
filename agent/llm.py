"""Provider-neutral LLM port for the investigator (suspect ordering and explanations).

Callers depend on the `LLM` protocol only: complete(prompt, *, model, max_tokens) returns the
text and token counts. Implementations:
- GeminiLLM: the Gemini REST API over plain httpx (no SDK dependency). LLM_API_KEY is sent only
  in the x-goog-api-key header and never appears in errors.
- FakeLLM: deterministic, scripted answers for tests.
- DisabledLLM: when LLM_LIVE=0 or no provider is configured. It always raises LLMUnavailable.

LLMUnavailable covers "no answer, use the fallback": quota (402/429/RESOURCE_EXHAUSTED),
network errors, disabled. Callers fall back to diff order or a template, so the UI never shows
a raw provider error.

The demo agents' own LLM loop is Khyati's (sandbox/llm.py). This port serves only the
investigator.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMUnavailable(Exception):
    """No usable answer (quota, payment, network, disabled). Use the fallback."""


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    model: str


class LLM(Protocol):
    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion: ...


class FakeLLM:
    def __init__(self, answers: Sequence[str | Exception]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion:
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return Completion(answer, len(prompt.split()), len(answer.split()), model)


class DisabledLLM:
    def __init__(self, reason: str):
        self.reason = reason

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion:
        raise LLMUnavailable(f"LLM disabled ({self.reason})")


class GeminiLLM:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None,
                 timeout: float = 30.0):
        if not api_key:
            raise ValueError("LLM_API_KEY is not set")
        self._key = api_key
        self._http = client or httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return "GeminiLLM()"

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion:
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0}}
        try:
            r = self._http.post(GEMINI_URL.format(model=model), json=body,
                                headers={"x-goog-api-key": self._key})
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"LLM unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            quota = r.status_code in (402, 429) or "RESOURCE_EXHAUSTED" in r.text \
                or "quota" in r.text.lower()
            reason = "quota or billing limit" if quota else "error"
            raise LLMUnavailable(f"LLM {reason} (HTTP {r.status_code})")
        try:
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            usage = data.get("usageMetadata", {})
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMUnavailable("LLM returned an unexpected body") from None
        return Completion(text, int(usage.get("promptTokenCount", 0)),
                          int(usage.get("candidatesTokenCount", 0)), model)


def llm_from_env(env: Mapping[str, str] | None = None) -> LLM:
    e = os.environ if env is None else env
    if e.get("LLM_LIVE", "1") == "0":
        return DisabledLLM("LLM_LIVE=0")
    if e.get("LLM_PROVIDER", "").lower() == "gemini" and e.get("LLM_API_KEY"):
        return GeminiLLM(e["LLM_API_KEY"])
    return DisabledLLM("no LLM provider configured")


def model_id(flag_value: str, env: Mapping[str, str] | None = None) -> str:
    """Map a provider-neutral flag value (model_cheap / model_flagship) to a real model id."""
    e = os.environ if env is None else env
    name = {"model_cheap": "MODEL_CHEAP", "model_flagship": "MODEL_FLAGSHIP"}.get(flag_value)
    if not name or not e.get(name):
        raise ValueError(f"no model id configured for {flag_value!r} (set {name})")
    return e[name]
