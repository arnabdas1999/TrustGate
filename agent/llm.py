"""Provider-neutral LLM port for the investigator (suspect ordering and explanations).

Callers depend on the `LLM` protocol only: complete(prompt, *, model, max_tokens) returns the
text and token counts. Implementations:
- GeminiLLM: the Gemini REST API over plain httpx (no SDK dependency). LLM_API_KEY is sent only
  in the x-goog-api-key header and never appears in errors.
- OpenAICompatLLM: /chat/completions over httpx, for Ollama (local, no key), Groq or any
  OpenAI-compatible API. <think> blocks from local reasoning models are stripped.
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
import re
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


# Ollama models that think before answering; think=false keeps the whole budget for the answer.
THINKING_MODELS = ("qwen3", "deepseek-r1")

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

# LLM_PROVIDER -> default LLM_BASE_URL for the OpenAI-compatible providers.
OPENAI_COMPAT_BASES = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "",
}


class OpenAICompatLLM:
    """POST {base_url}/chat/completions: Ollama (no key), Groq, or any OpenAI-compatible API.
    A Bearer header is sent only when there is a key, and the key never appears in errors."""

    def __init__(self, base_url: str, api_key: str = "", *, client: httpx.Client | None = None,
                 timeout: float = 120.0, reasoning_effort: str = ""):
        if not base_url:
            raise ValueError("LLM_BASE_URL is not set")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.reasoning_effort = reasoning_effort
        self._http = client or httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"OpenAICompatLLM({self.url!r})"

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion:
        body: dict = {"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            r = self._http.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"LLM unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            quota = r.status_code in (402, 429) or "quota" in r.text.lower() \
                or "rate limit" in r.text.lower()
            reason = "quota or rate limit" if quota else "error"
            raise LLMUnavailable(f"LLM {reason} (HTTP {r.status_code})")
        try:
            data = r.json()
            text = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMUnavailable("LLM returned an unexpected body") from None
        return Completion(_THINK.sub("", text).strip(), int(usage.get("prompt_tokens", 0)),
                          int(usage.get("completion_tokens", 0)), model)


class OllamaLLM:
    """Ollama's native POST {base}/api/chat, stream=false, num_predict=max_tokens, temperature 0.
    Thinking models (THINKING_MODELS, e.g. qwen3) get think=false so the answer isn't spent on
    thoughts; other models aren't sent the setting. No key."""

    def __init__(self, base_url: str = "http://localhost:11434", *,
                 client: httpx.Client | None = None, timeout: float = 120.0,
                 think: bool = False):
        base = base_url.rstrip("/")
        base = base[:-3] if base.endswith("/v1") else base
        self.url = base
        self.think = think
        self._http = client or httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"OllamaLLM({self.url!r})"

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> Completion:
        body: dict[str, object] = {
            "model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0}}
        if model.startswith(THINKING_MODELS):  # other models don't take the setting
            body["think"] = self.think
        try:
            r = self._http.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"LLM unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            hint = " (model not pulled? run `ollama pull`)" if r.status_code == 404 else ""
            raise LLMUnavailable(f"LLM error (HTTP {r.status_code}){hint}")
        try:
            data = r.json()
            text = data["message"]["content"]
            if not isinstance(text, str):
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LLMUnavailable("LLM returned an unexpected body") from None
        return Completion(_THINK.sub("", text).strip(), int(data.get("prompt_eval_count", 0)),
                          int(data.get("eval_count", 0)), model)


def llm_from_env(env: Mapping[str, str] | None = None) -> LLM:
    """LLM_PROVIDER picks the investigator's LLM:
    - gemini: LLM_API_KEY required
    - ollama: native /api/chat, think=false (LLM_THINK=1 to allow reasoning). LLM_BASE_URL or
      OLLAMA_URL, default http://localhost:11434 (in Docker http://host.docker.internal:11434;
      a trailing /v1 is ignored). No key
    - groq: LLM_API_KEY required, base default https://api.groq.com/openai/v1
    - openai: any OpenAI-compatible API, LLM_BASE_URL required
    LLM_TIMEOUT_S overrides the timeout (default 120 s for ollama, 30 s otherwise) and
    LLM_REASONING_EFFORT (e.g. "none") is passed through for reasoning models. Anything
    missing gives DisabledLLM, so the orchestrator falls back instead of failing."""
    e = os.environ if env is None else env
    if e.get("LLM_LIVE", "1") == "0":
        return DisabledLLM("LLM_LIVE=0")
    name = e.get("LLM_PROVIDER", "").strip().lower()
    key = e.get("LLM_API_KEY", "").strip()
    if name == "gemini" and key:
        return GeminiLLM(key)
    if name == "ollama":
        base = (e.get("LLM_BASE_URL", "").strip() or e.get("OLLAMA_URL", "").strip()
                or "http://localhost:11434")
        try:
            timeout = float(e.get("LLM_TIMEOUT_S") or 120)
        except ValueError:
            timeout = 120.0
        return OllamaLLM(base, timeout=timeout, think=e.get("LLM_THINK", "0") == "1")
    if name in OPENAI_COMPAT_BASES:
        base = e.get("LLM_BASE_URL", "").strip() or OPENAI_COMPAT_BASES[name]
        if not base:
            return DisabledLLM(f"LLM_PROVIDER={name} needs LLM_BASE_URL")
        if name == "groq" and not key:
            return DisabledLLM("LLM_PROVIDER=groq needs LLM_API_KEY")
        try:
            timeout = float(e.get("LLM_TIMEOUT_S") or 30)
        except ValueError:
            timeout = 30.0
        return OpenAICompatLLM(base, key, timeout=timeout,
                               reasoning_effort=e.get("LLM_REASONING_EFFORT", "").strip())
    return DisabledLLM("no LLM provider configured")


def model_id(flag_value: str, env: Mapping[str, str] | None = None) -> str:
    """Map a provider-neutral flag value (model_cheap / model_flagship) to a real model id."""
    e = os.environ if env is None else env
    name = {"model_cheap": "MODEL_CHEAP", "model_flagship": "MODEL_FLAGSHIP"}.get(flag_value)
    if not name or not e.get(name):
        raise ValueError(f"no model id configured for {flag_value!r} (set {name})")
    return e[name]
