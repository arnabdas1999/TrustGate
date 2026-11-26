"""Minimal streamable-HTTP MCP client for the hosted Flagsmith MCP server.

Hand-rolled on httpx (already a dependency) so the gate needs no extra package. It speaks
only what the gate uses: initialize, notifications/initialized, ping, tools/list and
tools/call, with JSON or SSE responses (checked against https://mcp.flagsmith.com/mcp on Nov 26).

Safety rules:
- The organisation key comes only from the gate's own environment (FLAGSMITH_API_KEY), is sent
  only in the Authorization header, and never appears in errors, logs or repr.
- Reads retry on network errors only, a few times. Writes are sent exactly once: a timeout
  on a write may mean it was applied, so the executor reports it instead of retrying.
- An HTTP 404 on a request that carried our mcp-session-id means the server no longer knows
  the session and did not run the request. The client then opens a new session once and
  resends; this is the one resend allowed for writes, because nothing was executed.
- Session setup is serialised by a lock, so concurrent callers open one session, not several.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_MCP_URL = "https://mcp.flagsmith.com/mcp"
PROTOCOL_VERSION = "2025-06-18"
METHOD_NOT_FOUND = -32601  # JSON-RPC error code


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
    def __init__(self, url: str, api_key: str, *, timeout: float = 10.0, read_retries: int = 2,
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
        self._ready = False  # initialize and notifications/initialized both done
        self._lock = threading.Lock()
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
        headers = self._headers()
        try:
            response = self._http.post(self.url, headers=headers, json=message)
        except httpx.TransportError as exc:
            raise _NetworkError(f"network error talking to MCP: {type(exc).__name__}") from None
        sent_session = headers.get("mcp-session-id")
        if response.status_code == 404 and sent_session:
            raise _SessionExpired(sent_session, f"MCP {message.get('method')} returned HTTP 404: "
                                                "session expired or unknown")
        if response.status_code >= 400:
            raise McpError(f"MCP {message.get('method')} returned HTTP {response.status_code}")
        if sid := response.headers.get("mcp-session-id"):
            self._session_id = sid
        return response

    def _rpc(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        reply = _find_reply(self._post(message), request_id)
        if "error" in reply:
            err = reply["error"]
            raise _RpcError(err.get("code"),
                            f"MCP {method} error {err.get('code')}: {err.get('message')}")
        return reply.get("result") or {}

    def _open_session(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:  # another caller opened it while we waited
                return
            self._session_id = self._protocol = None
            try:
                result = self._rpc("initialize", {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "trustgate-gate", "version": "0.1"}})
                self._protocol = result.get("protocolVersion", PROTOCOL_VERSION)
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except _SessionExpired:
                self._session_id = self._protocol = None
                raise McpError("MCP session was dropped while it was being opened") from None
            except BaseException:
                self._session_id = self._protocol = None
                raise
            self._ready = True

    def _drop_session(self, stale_session: str) -> None:
        with self._lock:
            if self._session_id == stale_session:  # not already replaced by another caller
                self._session_id = self._protocol = None
                self._ready = False

    def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """One request on an open session, re-opening it once if the server forgot it."""
        # Opening the session is safe to retry even before a write: nothing is written yet.
        self._with_retries(self._open_session)
        try:
            return self._rpc(method, params)
        except _SessionExpired as exc:
            self._drop_session(exc.session_id)
        self._with_retries(self._open_session)
        try:
            return self._rpc(method, params)
        except _SessionExpired:
            raise McpError(f"MCP {method}: session expired again right after "
                           "re-initialising") from None

    # -- public ----------------------------------------------------------------------

    def ensure_session(self) -> None:
        """Make sure a live session exists: open one if needed, then ping it.

        A ping that gets HTTP 404 re-opens the session once. Call this before consuming an
        approval so a dead session fails here, not after the approval is burned. Raises
        McpError if no working session can be established.
        """
        try:
            self._with_retries(self._ping)
        except _NetworkError as exc:
            raise McpError(f"{exc} during ping") from None

    def _ping(self) -> None:
        try:
            self._request("ping", None)
        except _RpcError as exc:
            # "Method not found" was still answered on our session, so the session is alive.
            if exc.code != METHOD_NOT_FOUND:
                raise

    def call_tool(self, name: str, arguments: dict[str, Any], *, is_write: bool) -> ToolResult:
        attempts = 1 if is_write else 1 + self._read_retries
        for attempt in range(attempts):
            try:
                result = self._request("tools/call", {"name": name, "arguments": arguments})
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
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def _with_retries(self, fn: Callable[[], None]) -> None:
        for attempt in range(1 + self._read_retries):
            try:
                fn()
                return
            except _NetworkError:
                if attempt == self._read_retries:
                    raise
                time.sleep(self._backoff_s * (attempt + 1))


class _NetworkError(McpError):
    pass


class _SessionExpired(McpError):
    """HTTP 404 on a request that carried this session id: the server did not run it."""

    def __init__(self, session_id: str, message: str):
        super().__init__(message)
        self.session_id = session_id


class _RpcError(McpError):
    """A JSON-RPC error reply (the server answered, so the session itself is fine)."""

    def __init__(self, code: Any, message: str):
        super().__init__(message)
        self.code = code


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
