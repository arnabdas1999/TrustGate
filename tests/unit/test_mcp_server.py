import io
import json

import httpx
import pytest

from gate.allowlist import GATE_NATIVE_READS, MCP_READS, MCP_WRITES
from mcp_server.server import TrustGateServer, serve
from mcp_server.tools import READ_TOOLS, TOOLS, Backend

VIEW = {"investigation": {"id": "inv_1", "status": "proposed", "segment": "tier0_production",
                          "suspects": [{"flag": "agent_prompt_version", "old": "v1_strict",
                                        "new": "v2_fast"}],
                          "culprit_set": [{"flag": "agent_prompt_version", "old": "v1_strict",
                                           "new": "v2_fast"}],
                          "runs_used": 52, "usd_used": 0.4, "proposal_id": "prop_1"},
        "proposal": {"id": "prop_1", "state": "proposed", "explanation": "why",
                     "expires_at": "2026-11-26T19:15:00Z",
                     "candidate": {"kind": "segment_override", "ops": [
                         {"tool": "create_segment_override", "feature": "agent_prompt_version",
                          "segment": "tier0_production", "value": "v1_strict"}]}},
        "active_ms": 0}


class Recorder:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append((request.method, request.url.path,
                              json.loads(request.content) if request.content else None))
        path = request.url.path
        if path == "/api/investigations" and request.method == "POST":
            return httpx.Response(201, json={"id": "inv_1", "status": "capturing"})
        if path == "/api/investigations/inv_1":
            return httpx.Response(200, json=VIEW)
        if path == "/gate/read":
            return httpx.Response(200, json={"tool": "x", "is_error": False,
                                             "result": {"results": [1, 2]}})
        return httpx.Response(404, json={"detail": "nope"})


@pytest.fixture
def setup():
    rec = Recorder()
    http = httpx.Client(transport=httpx.MockTransport(rec))
    backend = Backend(api_url="http://api:8000", gate_url="http://gate:8100",
                      web_url="http://localhost:3000", client=http)
    return TrustGateServer(backend), rec


def _call(server, name, args):
    return server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]


def test_tool_list_has_no_write_tools():
    names = {t["name"] for t in TOOLS}
    assert names == {"investigate_incident", "get_investigation", "propose_change"} \
        | READ_TOOLS
    assert not names & MCP_WRITES
    assert not names & {"create_segment_override", "update_feature_state",
                        "delete_feature_segment", "approve", "approve_proposal"}
    for name in names:
        assert not any(w in name for w in ("create", "update", "delete", "publish", "approve"))


def test_read_tools_are_exactly_the_gate_allowlist():
    assert READ_TOOLS == MCP_READS | GATE_NATIVE_READS


def test_initialize_and_list(setup):
    server, _ = setup
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})["result"]
    assert init["serverInfo"]["name"] == "trustgate" and "tools" in init["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
    assert {t["name"] for t in listed["tools"]} == {t["name"] for t in TOOLS}
    assert all("inputSchema" in t for t in listed["tools"])


def test_investigate_incident_starts_via_the_api(setup):
    server, rec = setup
    result = _call(server, "investigate_incident", {"incident_id": "inc_1"})
    assert not result["isError"]
    assert rec.requests == [("POST", "/api/investigations",
                             {"incident_id": "inc_1", "mode": "guided"})]
    assert "inv_1" in result["content"][0]["text"]


def test_get_investigation_summarises(setup):
    server, _ = setup
    text = _call(server, "get_investigation", {"investigation_id": "inv_1"})["content"][0]["text"]
    body = json.loads(text)
    assert body["status"] == "proposed" and body["culprit_set"][0]["flag"] == \
        "agent_prompt_version"


def test_propose_change_returns_an_approval_link_and_never_writes(setup):
    server, rec = setup
    result = _call(server, "propose_change", {"investigation_id": "inv_1"})
    body = json.loads(result["content"][0]["text"])
    assert body["approval_link"] == "http://localhost:3000/investigations/inv_1"
    assert body["needs_human_approval"] is True
    assert body["proposed_ops"][0]["tool"] == "create_segment_override"
    paths = [p for _, p, _ in rec.requests]
    assert not any(p.endswith("/gate/write") or p.endswith("/approve") for p in paths)
    assert all(m == "GET" for m, _, _ in rec.requests)


def test_propose_change_without_a_proposal_says_why(setup):
    server, rec = setup
    VIEW_NO = {**VIEW, "proposal": None,
               "investigation": {**VIEW["investigation"], "status": "searching"}}
    rec_view = VIEW_NO

    def handler(request):
        return httpx.Response(200, json=rec_view)

    backend = Backend(api_url="http://api:8000", gate_url="http://gate:8100",
                      web_url="http://localhost:3000",
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = _call(TrustGateServer(backend), "propose_change", {"investigation_id": "inv_1"})
    assert result["isError"] and "searching" in result["content"][0]["text"]


def test_allowlisted_read_goes_through_the_gate(setup):
    server, rec = setup
    result = _call(server, "list_project_features", {"project_pk": 56664})
    assert not result["isError"]
    assert rec.requests == [("POST", "/gate/read", {"tool": "list_project_features",
                                                    "args": {"project_pk": 56664}})]


def test_unknown_tool_is_an_error_not_a_call(setup):
    server, rec = setup
    for name in ("create_segment_override", "list_organizations", "approve"):
        result = _call(server, name, {})
        assert result["isError"] and rec.requests == []


def test_api_errors_become_tool_errors(setup):
    server, _ = setup
    result = _call(server, "get_investigation", {"investigation_id": "missing"})
    assert result["isError"] and "404" in result["content"][0]["text"]


def test_unknown_method_is_a_json_rpc_error(setup):
    server, _ = setup
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert reply["error"]["code"] == -32601


def test_stdio_loop_reads_and_writes_lines(setup):
    server, _ = setup
    stdin = io.StringIO("\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]) + "\nnot json\n")
    stdout = io.StringIO()
    serve(server, stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700


def test_read_tool_descriptions_name_their_required_scope_argument():
    from gate.allowlist import REQUIRED_SCOPE_ARGS

    by_name = {t["name"]: t for t in TOOLS}
    for tool, arg in REQUIRED_SCOPE_ARGS.items():
        if arg:
            assert f"'{arg}'" in by_name[tool]["description"], tool
