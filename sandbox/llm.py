"""Provider-neutral tool-calling loop for the demo agents.

The loop takes the model, temperature and step limit from the flags, records every tool call,
and accounts tokens and cost. Providers sit behind a small Chat protocol:
- GeminiProvider: REST via httpx, retries on 429/5xx.
- OllamaProvider: Ollama's native /api/chat (stream false, think false by default), which gave
  the team reliable tool calls with qwen3 / llama3.2 on a Mac. Same retries.
- OpenAICompatProvider: any OpenAI-compatible /chat/completions API (Groq as backup, or
  another). Same retries.
- ScriptedProvider: deterministic, for unit tests (no network, no credits).
The Ollama and OpenAI-compatible chats strip <think> blocks that reasoning models leave in text.

provider_from_env reads:
- LLM_PROVIDER: gemini (default), ollama, groq or openai
- LLM_BASE_URL: overrides the base URL. ollama: LLM_BASE_URL, else OLLAMA_URL, else
  http://localhost:11434 (in Docker use http://host.docker.internal:11434; a trailing /v1 is
  dropped). groq: default https://api.groq.com/openai/v1. openai: required
- LLM_API_KEY: required for gemini and groq, optional for ollama and openai (sent as Bearer)
- LLM_TIMEOUT_S: per-request HTTP timeout, default 300 s for ollama (local models are slow),
  90 s otherwise
- LLM_THINK=1: let an Ollama reasoning model think (default off: slower, and no better here)
The sandbox also scales its own limits for a local model (see run_timeout_s and
sandbox/runner.default_parallelism): SANDBOX_RUN_TIMEOUT_S and SANDBOX_PARALLEL override them.

Flag values model_cheap / model_flagship map to real model IDs via MODEL_CHEAP / MODEL_FLAGSHIP.
Prices (USD per 1M tokens, "in,out") come from PRICE_MODEL_CHEAP / PRICE_MODEL_FLAGSHIP, with
defaults below. Cost is measured, never estimated from text length. Local models are charged the
same notional prices (nothing is billed) so cost comparisons between flag values stay meaningful.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from contracts.models import ToolCall

# Defaults per 1M tokens (input, output). Override with PRICE_MODEL_CHEAP="0.30,2.50".
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "model_cheap": (0.30, 2.50),
    "model_flagship": (1.25, 10.00),
}


class LLMError(Exception):
    pass


class LLMHTTPError(LLMError):
    """A non-retryable (or retries exhausted) HTTP error; detail is the provider's message."""

    def __init__(self, message: str, *, status: int, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (type object)


@dataclass
class Call:
    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass
class Turn:
    text: str = ""
    calls: list[Call] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


class Chat(Protocol):
    def send(self, results: Sequence[tuple[Call, Any]] | None) -> Turn:
        """First call with None starts the conversation; later calls return tool results."""
        ...


class Provider(Protocol):
    def open_chat(self, *, model: str, system: str, user: str, tools: Sequence[ToolSpec],
                  temperature: float, seed: int | None = None) -> Chat: ...


# ---------------------------------------------------------------------------
# Models and prices
# ---------------------------------------------------------------------------


def model_id(flag_value: str, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    key = {"model_cheap": "MODEL_CHEAP", "model_flagship": "MODEL_FLAGSHIP"}.get(flag_value)
    if key is None:
        raise LLMError(f"unknown agent_model value {flag_value!r}")
    value = env.get(key, "")
    if not value:
        raise LLMError(f"{key} is not set (real model ID behind {flag_value})")
    return value


def price(flag_value: str, env: Mapping[str, str] | None = None) -> tuple[float, float]:
    env = os.environ if env is None else env
    raw = env.get(f"PRICE_{flag_value.upper()}", "")
    if raw:
        cost_in, cost_out = (float(x) for x in raw.split(","))
        return cost_in, cost_out
    return DEFAULT_PRICES[flag_value]


def cost_usd(flag_value: str, tokens_in: int, tokens_out: int,
             env: Mapping[str, str] | None = None) -> float:
    cost_in, cost_out = price(flag_value, env)
    return (tokens_in * cost_in + tokens_out * cost_out) / 1_000_000


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class LoopResult:
    tool_calls: list[ToolCall]
    final_text: str
    tokens_in: int
    tokens_out: int
    steps: int
    hit_step_limit: bool
    error: str | None = None


def run_tool_loop(chat: Chat, dispatch: Callable[[str, dict[str, Any]], Any], *,
                  max_steps: int, timeout_s: float = 120.0,
                  clock: Callable[[], float] = time.monotonic) -> LoopResult:
    """One model turn per step. Stops when the model answers without tool calls, at the step
    limit, or at the timeout. Tool errors go back to the model as results, not exceptions."""
    calls: list[ToolCall] = []
    tokens_in = tokens_out = steps = 0
    results: list[tuple[Call, Any]] | None = None
    text = ""
    start = clock()
    error: str | None = None
    hit_limit = True
    for _ in range(max_steps):
        if clock() - start > timeout_s:
            error = f"timeout after {timeout_s:.0f}s"
            hit_limit = False
            break
        try:
            turn = chat.send(results)
        except LLMError as exc:
            error = str(exc)
            hit_limit = False
            break
        steps += 1
        tokens_in += turn.tokens_in
        tokens_out += turn.tokens_out
        text = turn.text or text
        if not turn.calls:
            hit_limit = False
            break
        results = []
        for call in turn.calls:
            try:
                out = dispatch(call.name, call.args)
            except Exception as exc:  # noqa: BLE001 (any tool failure is the model's problem)
                out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            calls.append(ToolCall(name=call.name, args=call.args, result=out,
                                  ts=datetime.now(UTC)))
            results.append((call, out))
    return LoopResult(tool_calls=calls, final_text=text, tokens_in=tokens_in,
                      tokens_out=tokens_out, steps=steps, hit_step_limit=hit_limit, error=error)


# ---------------------------------------------------------------------------
# Scripted provider (tests)
# ---------------------------------------------------------------------------


class ScriptedChat:
    def __init__(self, turns: Sequence[Turn]):
        self.turns = list(turns)
        self.received: list[Sequence[tuple[Call, Any]] | None] = []

    def send(self, results: Sequence[tuple[Call, Any]] | None) -> Turn:
        self.received.append(results)
        if not self.turns:
            return Turn(text="done")
        return self.turns.pop(0)


class ScriptedProvider:
    """Plays back a fixed list of turns per chat. `script` may be a callable of the chat
    arguments so a test can react to the prompt or tool set."""

    def __init__(self, script: Sequence[Turn] | Callable[..., Sequence[Turn]]):
        self.script = script
        self.opened: list[dict[str, Any]] = []

    def open_chat(self, **kwargs: Any) -> ScriptedChat:
        self.opened.append(kwargs)
        turns = self.script(**kwargs) if callable(self.script) else self.script
        return ScriptedChat(turns)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUS = {429, 500, 502, 503, 504}


def _gemini_schema(schema: Any) -> Any:
    """JSON schema -> Gemini's OpenAPI subset (upper-case types, no unsupported keys)."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in ("additionalProperties", "$schema", "default"):
                continue
            out[key] = value.upper() if key == "type" and isinstance(value, str) \
                else _gemini_schema(value)
        return out
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


class GeminiChat:
    def __init__(self, provider: GeminiProvider, *, model: str, system: str, user: str,
                 tools: Sequence[ToolSpec], temperature: float, seed: int | None):
        self.provider = provider
        self.model = model
        self.contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": user}]}]
        self.body_base: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": [
                {"name": t.name, "description": t.description,
                 "parameters": _gemini_schema(t.parameters)} for t in tools]}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": {"temperature": temperature,
                                 **({"seed": seed} if seed is not None else {})},
        }

    def send(self, results: Sequence[tuple[Call, Any]] | None) -> Turn:
        if results:
            parts = []
            for call, out in results:
                response: dict[str, Any] = {"name": call.name,
                                            "response": out if isinstance(out, dict)
                                            else {"result": out}}
                if call.id:
                    response["id"] = call.id
                parts.append({"functionResponse": response})
            self.contents.append({"role": "user", "parts": parts})
        data = self.provider.generate(self.model, {**self.body_base, "contents": self.contents})
        candidates = data.get("candidates") or []
        if not candidates or "content" not in candidates[0]:
            reason = (data.get("promptFeedback") or {}).get("blockReason") \
                or (candidates[0].get("finishReason") if candidates else "no candidates")
            raise LLMError(f"Gemini returned no content ({reason})")
        content = candidates[0]["content"]
        # Keep the model's turn exactly as returned: thought signatures must be echoed back.
        self.contents.append({"role": "model", "parts": content.get("parts") or []})
        usage = data.get("usageMetadata") or {}
        turn = Turn(tokens_in=int(usage.get("promptTokenCount", 0)),
                    tokens_out=int(usage.get("candidatesTokenCount", 0))
                    + int(usage.get("thoughtsTokenCount", 0)))
        texts = []
        for part in content.get("parts") or []:
            if "functionCall" in part:
                fc = part["functionCall"]
                turn.calls.append(Call(name=fc["name"], args=dict(fc.get("args") or {}),
                                       id=fc.get("id")))
            elif "text" in part and not part.get("thought"):
                texts.append(part["text"])
        turn.text = "".join(texts)
        return turn


class GeminiProvider:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None,
                 retries: int = 4, timeout_s: float = 90.0,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise LLMError("LLM_API_KEY is not set")
        self._key = api_key
        self.client = client or httpx.Client(timeout=timeout_s)
        self.retries = retries
        self.sleep = sleep

    def __repr__(self) -> str:  # never show the key
        return "GeminiProvider()"

    def open_chat(self, *, model: str, system: str, user: str, tools: Sequence[ToolSpec],
                  temperature: float, seed: int | None = None) -> GeminiChat:
        return GeminiChat(self, model=model, system=system, user=user, tools=tools,
                          temperature=temperature, seed=seed)

    def generate(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        return _post_json(self.client, GEMINI_URL.format(model=model), body,
                          headers={"x-goog-api-key": self._key}, label="Gemini",
                          retries=self.retries, sleep=self.sleep, secret=self._key)


def _post_json(client: httpx.Client, url: str, body: dict[str, Any], *, headers: dict[str, str],
               label: str, retries: int, sleep: Callable[[float], None],
               secret: str = "") -> dict[str, Any]:
    """POST with retries on transport errors, 429 and 5xx (exponential backoff with jitter,
    honouring a numeric Retry-After). Errors never carry the key."""
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.TransportError as exc:
            if attempt == retries:
                raise LLMError(f"{label} unreachable: {type(exc).__name__}") from None
        else:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    raise LLMError(f"{label} returned a non-JSON 200") from None
                if not isinstance(data, dict):
                    raise LLMError(f"{label} returned a non-object 200")
                return data
            if resp.status_code not in RETRY_STATUS or attempt == retries:
                text = _error_text(resp)
                if secret:
                    text = text.replace(secret, "***")
                raise LLMHTTPError(f"{label} HTTP {resp.status_code}: {text}",
                                   status=resp.status_code, detail=text)
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        sleep(delay + random.uniform(0, delay / 2))  # noqa: S311 (jitter, not crypto)
        delay = min(delay * 2, 16.0)
    raise LLMError("unreachable")


def _error_text(resp: httpx.Response) -> str:
    """error.message (Gemini, OpenAI, Groq) or a plain error string (Ollama), truncated."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    return (_error_text_from(data) if isinstance(data, dict) else "") or resp.text[:300]


def _error_text_from(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))[:300]
    return str(error)[:300] if error else ""


# ---------------------------------------------------------------------------
# Shared by the Ollama and OpenAI-compatible chats
# ---------------------------------------------------------------------------

_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)


def _strip_think(text: str) -> str:
    """Drop <think>...</think> reasoning (qwen3 and other local reasoning models). An
    unterminated block (output cut off mid-thought) is dropped to the end."""
    return _THINK.sub("", text).strip()


def _parse_args(raw: Any, name: str) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise LLMError(f"tool call {name!r} has arguments that are not valid JSON") from None
        if isinstance(parsed, dict):
            return parsed
    raise LLMError(f"tool call {name!r} has arguments that are not a JSON object")


def _function_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.parameters}} for t in tools]


class _ToolCallParser:
    """Turns a response's tool_calls into Calls, giving each a stable id (call_<n>) when the
    server sent none. Returns the tool_calls to echo back: as received, plus any new id."""

    def __init__(self) -> None:
        self._n = 0

    def parse(self, tool_calls: Any, turn: Turn) -> list[dict[str, Any]]:
        echoed = []
        for item in tool_calls or []:
            raw = dict(item)
            fn = raw.get("function") or {}
            name = fn.get("name") or ""
            if not raw.get("id"):
                self._n += 1
                raw["id"] = f"call_{self._n}"
            echoed.append(raw)
            turn.calls.append(Call(name=name, args=_parse_args(fn.get("arguments"), name),
                                   id=raw["id"]))
        return echoed


def _text(content: Any) -> str:
    return _strip_think(content) if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions (Groq, OpenAI, ...)
# ---------------------------------------------------------------------------


class OpenAICompatChat:
    def __init__(self, provider: OpenAICompatProvider, *, model: str, system: str, user: str,
                 tools: Sequence[ToolSpec], temperature: float, seed: int | None):
        self.provider = provider
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                               {"role": "user", "content": user}]
        self.body_base: dict[str, Any] = {"model": model, "temperature": temperature}
        if tools:
            self.body_base["tools"] = _function_tools(tools)
            self.body_base["tool_choice"] = "auto"
        if seed is not None:
            self.body_base["seed"] = seed
        self._calls = _ToolCallParser()

    def send(self, results: Sequence[tuple[Call, Any]] | None) -> Turn:
        for call, out in results or ():
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": json.dumps(out, default=str)})
        data = self.provider.complete({**self.body_base, "messages": self.messages})
        choices = data.get("choices") or []
        first = choices[0] if isinstance(choices, list) and choices \
            and isinstance(choices[0], dict) else {}
        message = first.get("message")
        if not isinstance(message, dict):
            reason = _error_text_from(data) or first.get("finish_reason") or "no choices"
            raise LLMError(f"{self.provider.label} returned no message ({reason})")
        usage = data.get("usage") or {}
        turn = Turn(tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    text=_text(message.get("content")))
        echoed = self._calls.parse(message.get("tool_calls"), turn)
        assistant: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if echoed:
            assistant["tool_calls"] = echoed
        self.messages.append(assistant)
        return turn


class OpenAICompatProvider:
    """POST {base_url}/chat/completions. The key, if any, goes in a Bearer header and never
    appears in repr or errors."""

    def __init__(self, base_url: str, api_key: str = "", *, client: httpx.Client | None = None,
                 retries: int = 4, timeout_s: float = 90.0,
                 sleep: Callable[[float], None] = time.sleep, label: str = "LLM API"):
        if not base_url:
            raise LLMError("LLM_BASE_URL is not set")
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.client = client or httpx.Client(timeout=timeout_s)
        self.retries = retries
        self.sleep = sleep
        self.label = label

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatProvider({self.base_url!r})"

    def open_chat(self, *, model: str, system: str, user: str, tools: Sequence[ToolSpec],
                  temperature: float, seed: int | None = None) -> OpenAICompatChat:
        return OpenAICompatChat(self, model=model, system=system, user=user, tools=tools,
                                temperature=temperature, seed=seed)

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        return _post_json(self.client, f"{self.base_url}/chat/completions", body,
                          headers=headers, label=self.label, retries=self.retries,
                          sleep=self.sleep, secret=self._key)


# ---------------------------------------------------------------------------
# Ollama, native /api/chat (more reliable tool calls than its /v1 shim, and think=false)
# ---------------------------------------------------------------------------

OLLAMA_DEFAULT_URL = "http://localhost:11434"


# Ollama models that think before answering (same list as agent/llm.py); think=false keeps the
# whole budget for the answer and the tool calls.
THINKING_MODELS = ("qwen3", "deepseek-r1")


class OllamaChat:
    def __init__(self, provider: OllamaProvider, *, model: str, system: str, user: str,
                 tools: Sequence[ToolSpec], temperature: float, seed: int | None):
        self.provider = provider
        self.model = model
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                               {"role": "user", "content": user}]
        options: dict[str, Any] = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        self.body_base: dict[str, Any] = {"model": model, "stream": False, "options": options}
        if model.startswith(THINKING_MODELS):  # other models aren't sent the setting
            self.body_base["think"] = provider.think
        if tools:
            self.body_base["tools"] = _function_tools(tools)
        self._calls = _ToolCallParser()

    def send(self, results: Sequence[tuple[Call, Any]] | None) -> Turn:
        for call, out in results or ():
            self.messages.append({"role": "tool", "content": json.dumps(out, default=str),
                                  "tool_name": call.name})
        data = self.provider.chat(self.model, {**self.body_base, "messages": self.messages})
        message = data.get("message")
        if not isinstance(message, dict):
            reason = _error_text_from(data) or data.get("done_reason") or "no message"
            raise LLMError(f"Ollama returned no message ({reason})")
        turn = Turn(tokens_in=int(data.get("prompt_eval_count") or 0),
                    tokens_out=int(data.get("eval_count") or 0),
                    text=_text(message.get("content")))
        echoed = self._calls.parse(message.get("tool_calls"), turn)
        assistant: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if echoed:
            assistant["tool_calls"] = echoed
        self.messages.append(assistant)
        return turn


class OllamaProvider:
    """POST {base_url}/api/chat with stream=false. base_url may end in /v1 (the OpenAI shim's
    address); that suffix is dropped. No key needed; one, if given, is sent as Bearer (for a
    proxy in front of Ollama) and never appears in repr or errors."""

    def __init__(self, base_url: str = OLLAMA_DEFAULT_URL, api_key: str = "", *,
                 client: httpx.Client | None = None, retries: int = 4, timeout_s: float = 300.0,
                 sleep: Callable[[float], None] = time.sleep, think: bool = False):
        base = (base_url or OLLAMA_DEFAULT_URL).rstrip("/")
        self.base_url = base.removesuffix("/v1")
        self._key = api_key
        self.client = client or httpx.Client(timeout=timeout_s)
        self.retries = retries
        self.sleep = sleep
        self.think = think

    def __repr__(self) -> str:  # never show the key
        return f"OllamaProvider({self.base_url!r})"

    def open_chat(self, *, model: str, system: str, user: str, tools: Sequence[ToolSpec],
                  temperature: float, seed: int | None = None) -> OllamaChat:
        return OllamaChat(self, model=model, system=system, user=user, tools=tools,
                          temperature=temperature, seed=seed)

    def chat(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            return _post_json(self.client, f"{self.base_url}/api/chat", body, headers=headers,
                              label="Ollama", retries=self.retries, sleep=self.sleep,
                              secret=self._key)
        except LLMHTTPError as exc:
            if exc.status == 404 and "not found" in exc.detail.lower():
                raise LLMError(f"Ollama has no model {model!r}: run `ollama pull {model}` "
                               "on the Ollama host") from None
            raise


# ---------------------------------------------------------------------------
# Choosing a provider
# ---------------------------------------------------------------------------

OPENAI_COMPAT_BASES = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "",  # any OpenAI-compatible API: LLM_BASE_URL required
}
SUPPORTED_PROVIDERS = ("gemini", "ollama", *OPENAI_COMPAT_BASES)


def provider_name(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("LLM_PROVIDER", "gemini").strip().lower() or "gemini"


def _float_env(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise LLMError(f"{key}={raw!r} is not a number") from None


def run_timeout_s(env: Mapping[str, str] | None = None) -> float:
    """Wall-clock limit for one agent run (all its steps): SANDBOX_RUN_TIMEOUT_S, else 900 s
    for a local Ollama model and 120 s for hosted APIs."""
    env = os.environ if env is None else env
    return _float_env(env, "SANDBOX_RUN_TIMEOUT_S",
                      900.0 if provider_name(env) == "ollama" else 120.0)


def provider_from_env(env: Mapping[str, str] | None = None) -> Provider:
    env = os.environ if env is None else env
    name = provider_name(env)
    key = env.get("LLM_API_KEY", "").strip()
    base = env.get("LLM_BASE_URL", "").strip()
    timeout = _float_env(env, "LLM_TIMEOUT_S", 300.0 if name == "ollama" else 90.0)
    if name == "gemini":
        return GeminiProvider(key, timeout_s=timeout)
    if name == "ollama":
        think = env.get("LLM_THINK", "").strip().lower() in ("1", "true", "yes")
        return OllamaProvider(base or env.get("OLLAMA_URL", "").strip() or OLLAMA_DEFAULT_URL,
                              key, timeout_s=timeout, think=think)
    if name in OPENAI_COMPAT_BASES:
        base = base or OPENAI_COMPAT_BASES[name]
        if not base:
            raise LLMError(f"LLM_PROVIDER={name} needs LLM_BASE_URL")
        if name == "groq" and not key:
            raise LLMError("LLM_PROVIDER=groq needs LLM_API_KEY")
        return OpenAICompatProvider(base, key, timeout_s=timeout,
                                    label="Groq" if name == "groq" else "LLM API")
    raise LLMError(f"LLM_PROVIDER={name!r} is not supported ({', '.join(SUPPORTED_PROVIDERS)})")
