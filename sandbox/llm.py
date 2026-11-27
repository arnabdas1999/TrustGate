"""Provider-neutral tool-calling loop for the demo agents.

The loop takes the model, temperature and step limit from the flags, records every tool call,
and accounts tokens and cost. Providers sit behind a small Chat protocol:
- GeminiProvider: the team's provider (docs/decisions.md), REST via httpx, retries on 429/5xx.
- ScriptedProvider: deterministic, for unit tests (no network, no credits).

Flag values model_cheap / model_flagship map to real model IDs via MODEL_CHEAP / MODEL_FLAGSHIP.
Prices (USD per 1M tokens, "in,out") come from PRICE_MODEL_CHEAP / PRICE_MODEL_FLAGSHIP, with
defaults below. Cost is measured, never estimated from text length.
"""

from __future__ import annotations

import json
import os
import random
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
        url = GEMINI_URL.format(model=model)
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.post(url, json=body, headers={"x-goog-api-key": self._key})
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise LLMError(f"Gemini unreachable: {type(exc).__name__}") from None
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        raise LLMError("Gemini returned a non-JSON 200") from None
                if resp.status_code not in RETRY_STATUS or attempt == self.retries:
                    raise LLMError(f"Gemini HTTP {resp.status_code}: {_error_text(resp)}")
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            self.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311 (jitter, not crypto)
            delay = min(delay * 2, 16.0)
        raise LLMError("unreachable")


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except (json.JSONDecodeError, AttributeError):
        return resp.text[:300]


def provider_from_env(env: Mapping[str, str] | None = None) -> Provider:
    env = os.environ if env is None else env
    name = env.get("LLM_PROVIDER", "gemini").strip().lower() or "gemini"
    if name == "gemini":
        return GeminiProvider(env.get("LLM_API_KEY", ""))
    raise LLMError(f"LLM_PROVIDER={name!r} is not supported (gemini)")
