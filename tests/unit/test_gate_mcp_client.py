import json
import threading
import time

import httpx
import pytest

from gate.mcp_client import McpClient, McpError

KEY = "orgprefix.secretsecretsecret"
URL = "https://mcp.example/mcp"


class FakeServer:
    """Speaks just enough streamable-HTTP MCP: initialize, initialized, ping, tools/call.

    Each initialize opens a new session (sess-1, sess-2, ...). A request that carries an
    expired or unknown session id gets HTTP 404, like a real server that dropped the session.
    """

    def __init__(self, *, sse=True, fail_first=0, fail_exc=httpx.ConnectError,
                 tool_result=None, rpc_error=None, ping_error=None, init_status=200,
                 init_delay=0.0):
        self.sse = sse
        self.fail_left = fail_first
        self.fail_exc = fail_exc
        self.tool_result = tool_result or {"content": [{"type": "text", "text": '{"ok": 1}'}],
                                           "isError": False}
        self.rpc_error = rpc_error
        self.ping_error = ping_error
        self.init_status = init_status
        self.init_delay = init_delay
        self.requests = []
        self.sessions = 0
        self.expired = set()
        self.always_expire = False  # every session dies as soon as it is used
        self.timeout_on_session = None  # tools/call on this session id times out

    def expire(self):
        self.expired.add(f"sess-{self.sessions}")

    def _reply(self, payload, sid):
        if self.sse:
            body = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(200, text=body, headers={
                "content-type": "text/event-stream", "mcp-session-id": sid})
        return httpx.Response(200, json=payload, headers={"mcp-session-id": sid})

    def __call__(self, request):
        msg = json.loads(request.content)
        self.requests.append((msg.get("method"), dict(request.headers), msg))
        if msg.get("method") == "tools/call" and self.fail_left:
            self.fail_left -= 1
            raise self.fail_exc("network down", request=request)
        if msg.get("method") == "tools/call" and \
                request.headers.get("mcp-session-id") == self.timeout_on_session:
            raise httpx.ReadTimeout("slow", request=request)
        if msg["method"] == "initialize":
            if self.init_delay:
                time.sleep(self.init_delay)
            if self.init_status != 200:
                return httpx.Response(self.init_status)
            self.sessions += 1
            return self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "1"}}}, f"sess-{self.sessions}")
        sid = request.headers.get("mcp-session-id")
        if sid in self.expired:
            return httpx.Response(404, text="session not found")
        if msg["method"] == "notifications/initialized":
            return httpx.Response(202)
        if self.always_expire:
            self.expired.add(sid)
            return httpx.Response(404, text="session not found")
        if msg["method"] == "ping":
            if self.ping_error:
                return self._reply({"jsonrpc": "2.0", "id": msg["id"], "error": self.ping_error},
                                   sid)
            return self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": {}}, sid)
        if msg["method"] == "tools/call":
            if self.rpc_error:
                return self._reply({"jsonrpc": "2.0", "id": msg["id"], "error": self.rpc_error},
                                   sid)
            return self._reply({"jsonrpc": "2.0", "id": msg["id"], "result": self.tool_result},
                               sid)
        return httpx.Response(400)

    def calls(self):
        return [r for r in self.requests if r[0] == "tools/call"]

    def methods(self):
        return [r[0] for r in self.requests]


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


def test_list_tools_follows_pages():
    pages = {None: {"tools": [{"name": "a"}], "nextCursor": "c2"},
             "c2": {"tools": [{"name": "b"}]}}

    def handler(request):
        msg = json.loads(request.content)
        if msg["method"] == "tools/list":
            page = pages[(msg.get("params") or {}).get("cursor")]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": page})
        return FakeServer(sse=False)(request)

    client = McpClient(URL, KEY, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [t["name"] for t in client.list_tools()] == ["a", "b"]


def test_server_key_is_refused_as_the_org_key():
    with pytest.raises(McpError, match="organisation"):
        McpClient(URL, "ser.abcdef")


# --- expired sessions ------------------------------------------------------------------


@pytest.mark.parametrize("is_write", [False, True])
def test_expired_session_is_reopened_once_and_the_call_resent(is_write):
    server = FakeServer()
    client = _client(server, backoff_s=0)
    client.call_tool("a", {}, is_write=False)
    server.expire()
    result = client.call_tool("create_environment_feature_version" if is_write else "b", {},
                              is_write=is_write)
    assert not result.is_error
    assert server.methods() == ["initialize", "notifications/initialized", "tools/call",
                                "tools/call", "initialize", "notifications/initialized",
                                "tools/call"]
    stale, resent = server.calls()[1:]
    assert stale[1]["mcp-session-id"] == "sess-1"
    assert resent[1]["mcp-session-id"] == "sess-2"
    assert resent[2]["params"] == stale[2]["params"]


@pytest.mark.parametrize("is_write", [False, True])
def test_session_that_keeps_expiring_fails_after_one_reopen(is_write):
    server = FakeServer()
    server.always_expire = True
    with pytest.raises(McpError, match="session") as err:
        _client(server, backoff_s=0).call_tool("x", {}, is_write=is_write)
    assert len(server.calls()) == 2
    assert server.methods().count("initialize") == 2
    assert KEY not in str(err.value)


def test_404_without_a_session_is_not_treated_as_expiry():
    def handler(request):
        msg = json.loads(request.content)
        if msg["method"] == "initialize":  # a stateless server: no session id issued
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18"}})
        if msg["method"] == "tools/call":
            return httpx.Response(404)
        return httpx.Response(202)

    seen = []
    client = McpClient(URL, KEY, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(json.loads(r.content)["method"]) or handler(r))))
    with pytest.raises(McpError, match="404"):
        client.call_tool("w", {}, is_write=True)
    assert seen.count("tools/call") == 1 and seen.count("initialize") == 1


def test_network_error_on_a_resent_write_is_not_retried():
    server = FakeServer()
    client = _client(server, read_retries=5, backoff_s=0)
    client.call_tool("a", {}, is_write=False)
    server.expire()
    server.timeout_on_session = "sess-2"  # the resend after re-initialising times out
    with pytest.raises(McpError, match="not retried"):
        client.call_tool("create_environment_feature_version", {}, is_write=True)
    assert [c[1]["mcp-session-id"] for c in server.calls()] == ["sess-1", "sess-1", "sess-2"]


# --- ensure_session ----------------------------------------------------------------------


def test_ensure_session_opens_and_pings():
    server = FakeServer()
    client = _client(server)
    client.ensure_session()
    assert server.methods() == ["initialize", "notifications/initialized", "ping"]
    ping = server.requests[-1]
    assert ping[1]["mcp-session-id"] == "sess-1"
    assert ping[2]["jsonrpc"] == "2.0" and "id" in ping[2]


def test_ensure_session_on_a_live_session_only_pings():
    server = FakeServer()
    client = _client(server)
    client.call_tool("a", {}, is_write=False)
    client.ensure_session()
    assert server.methods()[-1] == "ping"
    assert server.methods().count("initialize") == 1


def test_ensure_session_reopens_a_dead_session_before_any_write():
    server = FakeServer()
    client = _client(server)
    client.call_tool("a", {}, is_write=False)
    server.expire()
    client.ensure_session()
    assert server.methods()[3:] == ["ping", "initialize", "notifications/initialized", "ping"]
    client.call_tool("create_environment_feature_version", {}, is_write=True)
    assert server.calls()[-1][1]["mcp-session-id"] == "sess-2"
    assert len(server.calls()) == 2


def test_ensure_session_raises_when_the_session_keeps_dying():
    server = FakeServer()
    server.always_expire = True
    with pytest.raises(McpError):
        _client(server).ensure_session()
    assert server.methods().count("initialize") == 2


@pytest.mark.parametrize("status", [401, 500])
def test_ensure_session_raises_when_initialize_fails(status):
    server = FakeServer(init_status=status)
    with pytest.raises(McpError, match=str(status)) as err:
        _client(server, backoff_s=0).ensure_session()
    assert KEY not in str(err.value)


def test_ensure_session_raises_on_network_errors():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client = McpClient(URL, KEY, backoff_s=0,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(McpError, match="network") as err:
        client.ensure_session()
    assert KEY not in str(err.value)


def test_ping_method_not_found_still_proves_the_session_is_alive():
    server = FakeServer(ping_error={"code": -32601, "message": "Method not found"})
    _client(server).ensure_session()


def test_ping_other_rpc_error_raises():
    server = FakeServer(ping_error={"code": -32603, "message": "internal"})
    with pytest.raises(McpError, match="internal"):
        _client(server).ensure_session()


def test_concurrent_callers_initialise_once():
    server = FakeServer(init_delay=0.05)
    client = _client(server)
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        barrier.wait()
        try:
            client.call_tool("a", {}, is_write=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert server.methods().count("initialize") == 1
    assert len(server.calls()) == 8
