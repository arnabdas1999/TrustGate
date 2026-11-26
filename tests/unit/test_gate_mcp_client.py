import json

import httpx
import pytest

from gate.mcp_client import McpClient, McpError

KEY = "orgprefix.secretsecretsecret"
URL = "https://mcp.example/mcp"


class FakeServer:
    """Speaks just enough streamable-HTTP MCP: initialize, initialized, tools/call."""

    def __init__(self, *, sse=True, fail_first=0, fail_exc=httpx.ConnectError,
                 tool_result=None, rpc_error=None):
        self.sse = sse
        self.fail_left = fail_first
        self.fail_exc = fail_exc
        self.tool_result = tool_result or {"content": [{"type": "text", "text": '{"ok": 1}'}],
                                           "isError": False}
        self.rpc_error = rpc_error
        self.requests = []

    def _reply(self, payload):
        if self.sse:
            body = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(200, text=body, headers={
                "content-type": "text/event-stream", "mcp-session-id": "sess-1"})
        return httpx.Response(200, json=payload, headers={"mcp-session-id": "sess-1"})

    def __call__(self, request):
        msg = json.loads(request.content)
        self.requests.append((msg.get("method"), dict(request.headers), msg))
        if msg.get("method") == "tools/call" and self.fail_left:
            self.fail_left -= 1
            raise self.fail_exc("network down", request=request)
        if msg["method"] == "initialize":
            return self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "1"}}})
        if msg["method"] == "notifications/initialized":
            return httpx.Response(202)
        if msg["method"] == "tools/call":
            if self.rpc_error:
                return self._reply({"jsonrpc": "2.0", "id": msg["id"], "error": self.rpc_error})
            return self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": self.tool_result})
        return httpx.Response(400)

    def calls(self):
        return [r for r in self.requests if r[0] == "tools/call"]


def _client(server, **kw):
    return McpClient(URL, KEY, client=httpx.Client(transport=httpx.MockTransport(server)), **kw)


@pytest.mark.parametrize("sse", [True, False])
def test_read_call_round_trip(sse):
    server = FakeServer(sse=sse)
    result = _client(server).call_tool("list_project_features", {"project_id": 1}, is_write=False)
    assert not result.is_error
    assert result.json() == {"ok": 1}
    methods = [r[0] for r in server.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]


def test_headers_carry_key_session_and_version():
    server = FakeServer()
    _client(server).call_tool("get_feature_flag", {}, is_write=False)
    _, headers, msg = server.calls()[0]
    assert headers["authorization"] == f"Api-Key {KEY}"
    assert headers["mcp-session-id"] == "sess-1"
    assert headers["mcp-protocol-version"] == "2025-06-18"
    assert "text/event-stream" in headers["accept"]
    assert msg["params"] == {"name": "get_feature_flag", "arguments": {}}


def test_session_is_reused_across_calls():
    server = FakeServer()
    client = _client(server)
    client.call_tool("a", {}, is_write=False)
    client.call_tool("b", {}, is_write=False)
    assert [r[0] for r in server.requests].count("initialize") == 1


def test_reads_retry_on_network_errors():
    server = FakeServer(fail_first=2)
    result = _client(server, read_retries=2, backoff_s=0).call_tool("x", {}, is_write=False)
    assert not result.is_error
    assert len(server.calls()) == 3


def test_reads_give_up_after_the_retry_budget():
    server = FakeServer(fail_first=5)
    with pytest.raises(McpError, match="network"):
        _client(server, read_retries=2, backoff_s=0).call_tool("x", {}, is_write=False)
    assert len(server.calls()) == 3


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_writes_are_never_retried(exc):
    server = FakeServer(fail_first=1, fail_exc=exc)
    with pytest.raises(McpError):
        _client(server, read_retries=5, backoff_s=0).call_tool(
            "create_environment_feature_version", {}, is_write=True)
    assert len(server.calls()) == 1


def test_tool_error_is_returned_not_raised():
    server = FakeServer(tool_result={"content": [{"type": "text", "text": "HTTP error 404"}],
                                     "isError": True})
    result = _client(server).call_tool("x", {}, is_write=False)
    assert result.is_error and "404" in result.text()


def test_json_rpc_error_raises():
    server = FakeServer(rpc_error={"code": -32602, "message": "bad params"})
    with pytest.raises(McpError, match="bad params"):
        _client(server).call_tool("x", {}, is_write=False)


def test_http_error_status_raises_without_retry_for_writes():
    def handler(request):
        msg = json.loads(request.content)
        if msg["method"] == "tools/call":
            return httpx.Response(500, text="boom")
        return FakeServer()(request)

    with pytest.raises(McpError, match="500"):
        McpClient(URL, KEY, client=httpx.Client(transport=httpx.MockTransport(handler))) \
            .call_tool("w", {}, is_write=True)


def test_key_never_appears_in_errors_or_repr():
    server = FakeServer(fail_first=9)
    client = _client(server, read_retries=0, backoff_s=0)
    assert KEY not in repr(client)
    with pytest.raises(McpError) as err:
        client.call_tool("x", {}, is_write=False)
    assert KEY not in str(err.value)


def test_from_env_needs_the_key():
    with pytest.raises(McpError, match="FLAGSMITH_API_KEY"):
        McpClient.from_env({})


def test_from_env_reads_key_and_url():
    client = McpClient.from_env({"FLAGSMITH_API_KEY": KEY, "FLAGSMITH_MCP_URL": URL})
    assert client.url == URL
    client.close()


def test_server_key_is_refused_as_the_org_key():
    with pytest.raises(McpError, match="organisation"):
        McpClient(URL, "ser.abcdef")
