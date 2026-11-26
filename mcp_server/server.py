"""TrustGate MCP server over stdio (newline-delimited JSON-RPC), so Claude Code can launch it.

    TRUSTGATE_API_URL=http://localhost:8000 GATE_URL=http://localhost:8100 \\
        uv run python -m mcp_server

Protocol stdout carries only JSON-RPC; logs go to stderr. See mcp_server/tools.py for the tools.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, TextIO

from mcp_server.tools import TOOLS, Backend, ToolError, call_tool, result_content

PROTOCOL_VERSION = "2025-06-18"
log = logging.getLogger("trustgate.mcp")


class TrustGateServer:
    def __init__(self, backend: Backend):
        self.backend = backend

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:  # notification: no reply
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except _MethodNotFound:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # noqa: BLE001 (one bad request must not kill the server)
            log.exception("internal error handling %s", method)
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"internal error: {type(exc).__name__}"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "trustgate", "version": "0.1.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name, args = params.get("name", ""), params.get("arguments") or {}
            try:
                return result_content(call_tool(self.backend, name, args), is_error=False)
            except (ToolError, KeyError) as exc:
                return result_content(f"{type(exc).__name__}: {exc}", is_error=True)
        raise _MethodNotFound


class _MethodNotFound(Exception):
    pass


def serve(server: TrustGateServer, stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(message) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    backend = Backend(api_url=os.environ.get("TRUSTGATE_API_URL", "http://localhost:8000"),
                      gate_url=os.environ.get("GATE_URL", "http://localhost:8100"),
                      web_url=os.environ.get("TRUSTGATE_WEB_URL", "http://localhost:3000"))
    serve(TrustGateServer(backend), sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
