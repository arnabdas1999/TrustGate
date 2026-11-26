"""Minimal streamable-HTTP MCP client for the hosted Flagsmith MCP server.

Hand-rolled on httpx (already a dependency) so the gate needs no extra package. It speaks
only what the gate uses: initialize, notifications/initialized and tools/call, with JSON or
SSE responses (checked against https://mcp.flagsmith.com/mcp on Nov 26).

Safety rules:
- The organisation key comes only from the gate's own environment (FLAGSMITH_API_KEY), is sent
  only in the Authorization header, and never appears in errors, logs or repr.
- Reads retry on network errors only, a few times. Writes are sent exactly once: a timeout
  on a write may mean it was applied, so the executor reports it instead of retrying.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_MCP_URL = "https://mcp.flagsmith.com/mcp"
PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    pass


@dataclass
class ToolResult:
    is_error: bool
    content: list[dict[str, Any]] = field(default_factory=list)
    structured: Any = None

    def text(self) -> str:
        return "\n".join(c.get("text", "") for c in self.content if c.get("type") == "text")

    def json(self) -> Any:
        if self.structured is not None:
            return self.structured
        return json.loads(self.text())


class McpClient:
    def __init__(self, url: str, api_key: str, *, timeout: float = 30.0, read_retries: int = 2,
                 backoff_s: float = 0.5, client: httpx.Client | None = None):
        if not api_key:
            raise McpError("FLAGSMITH_API_KEY is not set")
        if api_key.startswith("ser."):
            raise McpError("FLAGSMITH_API_KEY must be the organisation API key, not a ser. key")
        self.url = url
        self._key = api_key
        self._http = client or httpx.Client(timeout=timeout)
        self._read_retries = read_retries
        self._backoff_s = backoff_s
        self._session_id: str | None = None
        self._protocol: str | None = None
        self._ids = itertools.count(1)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> McpClient:
        env = os.environ if env is None else env
        return cls(env.get("FLAGSMITH_MCP_URL", DEFAULT_MCP_URL),
                   env.get("FLAGSMITH_API_KEY", ""), **kwargs)

    def __repr__(self) -> str:
        return f"McpClient(url={self.url!r})"

    def close(self) -> None:
        self._http.close()

    # -- protocol --------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Api-Key {self._key}",
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        if self._protocol:
            headers["mcp-protocol-version"] = self._protocol
        return headers

    def _post(self, message: dict[str, Any]) -> httpx.Response:
        try:
            response = self._http.post(self.url, headers=self._headers(), json=message)
        except httpx.TransportError as exc:
            raise _NetworkError(f"network error talking to MCP: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise McpError(f"MCP {message.get('method')} returned HTTP {response.status_code}")
        if sid := response.headers.get("mcp-session-id"):
            self._session_id = sid
        return response

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = next(self._ids)
        response = self._post({"jsonrpc": "2.0", "id": request_id, "method": method,
                               "params": params})
        message = _find_reply(response, request_id)
        if "error" in message:
            err = message["error"]
            raise McpError(f"MCP {method} error {err.get('code')}: {err.get('message')}")
        return message.get("result") or {}

    def _ensure_session(self) -> None:
        if self._protocol:
            return
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "trustgate-gate",
                                                         "version": "0.1"}})
        self._protocol = result.get("protocolVersion", PROTOCOL_VERSION)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # -- public ----------------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any], *, is_write: bool) -> ToolResult:
        attempts = 1 if is_write else 1 + self._read_retries
        for attempt in range(attempts):
            try:
                # Setting up the session is safe to retry even before a write: nothing written yet.
                self._with_retries(self._ensure_session)
                result = self._rpc("tools/call", {"name": name, "arguments": arguments})
                return ToolResult(is_error=bool(result.get("isError")),
                                  content=result.get("content") or [],
                                  structured=result.get("structuredContent"))
            except _NetworkError as exc:
                if attempt + 1 >= attempts:
                    what = "write (not retried)" if is_write else "read"
                    raise McpError(f"{exc} during {what} {name}") from None
                time.sleep(self._backoff_s * (attempt + 1))
        raise AssertionError("unreachable")

    def list_tools(self) -> list[dict[str, Any]]:
        """All tools the server exposes (a read; used by the hour-one integration check)."""
        self._with_retries(self._ensure_session)
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def _with_retries(self, fn) -> None:
        for attempt in range(1 + self._read_retries):
            try:
                return fn()
            except _NetworkError:
                if attempt == self._read_retries:
                    raise
                time.sleep(self._backoff_s * (attempt + 1))


class _NetworkError(McpError):
    pass


def _find_reply(response: httpx.Response, request_id: int) -> dict[str, Any]:
    if "text/event-stream" in response.headers.get("content-type", ""):
        messages = []
        for block in response.text.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines()
                             if line.startswith("data:"))
            if data:
                messages.append(json.loads(data))
    else:
        body = response.json()
        messages = body if isinstance(body, list) else [body]
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McpError(f"no MCP reply for request {request_id}")
