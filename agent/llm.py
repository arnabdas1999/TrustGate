"""Provider-neutral LLM port for the investigator (suspect ordering and explanations).

Callers depend on the `LLM` protocol only: complete(prompt, *, model, max_tokens) returns the
text and token counts. Implementations:
- GeminiLLM: the Gemini REST API over plain httpx (no SDK dependency). LLM_API_KEY is sent only
  in the x-goog-api-key header and never appears in errors.
- OllamaLLM: a local Ollama server (LLM_PROVIDER=ollama, OLLAMA_URL, default
  http://localhost:11434; no key). /api/chat with stream=false, num_predict=max_tokens,
  temperature 0, and think=false for thinking models (qwen3), so the answer isn't spent on
  thoughts.
- FakeLLM: deterministic, scripted answers for tests.
- DisabledLLM: when LLM_LIVE=0 or no provider is configured. It always raises LLMUnavailable.

LLMUnavailable covers "no answer, use the fallback": quota (402/429/RESOURCE_EXHAUSTED),
network errors, Ollama down or a model not pulled, disabled. Callers fall back to diff order
or a template, so the UI never shows a raw provider error.

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
OLLAMA_URL = "http://localhost:11434"
# Ollama models that think before answering; think=false keeps the whole budget for the answer.
THINKING_MODELS = ("qwen3", "deepseek-r1")


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


class OllamaLLM:
    def __init__(self, url: str = OLLAMA_URL, *, client: httpx.Client | None = None,
                 timeout: float = 120.0):
        self.url = (url or OLLAMA_URL).rstrip("/")
        self._http = client or httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"OllamaLLM({self.url!r})"

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion:
        body: dict[str, object] = {
            "model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0}}
        if model.startswith(THINKING_MODELS):
            body["think"] = False
        try:
            r = self._http.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"LLM unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            reason = "model not available (pull it first)" if r.status_code == 404 else "error"
            raise LLMUnavailable(f"LLM {reason} (HTTP {r.status_code})")
        try:
            data = r.json()
            text = data["message"]["content"]
            if not isinstance(text, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise LLMUnavailable("LLM returned an unexpected body") from None
        return Completion(text, int(data.get("prompt_eval_count") or 0),
                          int(data.get("eval_count") or 0), model)


def llm_from_env(env: Mapping[str, str] | None = None) -> LLM:
    e = os.environ if env is None else env
    if e.get("LLM_LIVE", "1") == "0":
        return DisabledLLM("LLM_LIVE=0")
    provider = e.get("LLM_PROVIDER", "").strip().lower()
    if provider == "gemini" and e.get("LLM_API_KEY"):
        return GeminiLLM(e["LLM_API_KEY"])
    if provider == "ollama":  # local: no key
        return OllamaLLM(e.get("OLLAMA_URL", "").strip() or OLLAMA_URL)
    return DisabledLLM("no LLM provider configured")


def model_id(flag_value: str, env: Mapping[str, str] | None = None) -> str:
    """Map a provider-neutral flag value (model_cheap / model_flagship) to a real model id."""
    e = os.environ if env is None else env
    name = {"model_cheap": "MODEL_CHEAP", "model_flagship": "MODEL_FLAGSHIP"}.get(flag_value)
    if not name or not e.get(name):
        raise ValueError(f"no model id configured for {flag_value!r} (set {name})")
    return e[name]
