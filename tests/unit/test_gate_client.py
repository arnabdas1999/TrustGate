import json

import httpx
import pytest

from contracts.models import AuditEntry, WriteOp
from orchestrator.gate_client import GateClient, GateError

OPS = [WriteOp(tool="create_segment_override", project="56664", environment="Development",
               feature="agent_prompt_version", segment="tier0_production", value="v1_strict")]
GATE_LINE = {"ts": "2026-11-26T18:20:11Z", "actor": "executor", "action": "write",
             "tool": "create_environment_feature_version", "args": {}, "result": "applied",
             "proposal_id": "prop_1", "approver": "naman", "prev_hash": "0" * 64}


def _client(handler):
    return GateClient("http://gate:8100",
                      client=httpx.Client(transport=httpx.MockTransport(handler),
                                          base_url="http://gate:8100"))


def test_fingerprint():
    def handler(request):
        assert request.url.path == "/gate/fingerprint"
        assert request.url.params["environment"] == "Development"
        return httpx.Response(200, json={"environment": "Development", "fingerprint": "f" * 64})

    assert _client(handler).fingerprint("Development") == "f" * 64


def test_render_posts_ops_and_returns_calls():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/gate/render"
        assert body["proposal_id"] == "prop_1"
        assert body["ops"][0]["feature"] == "agent_prompt_version"
        return httpx.Response(200, json={"calls": [{"tool": "create_environment_feature_version",
                                                    "arguments": {"feature_pk": 1}}]})

    calls = _client(handler).render(OPS, "prop_1")
    assert calls[0]["tool"] == "create_environment_feature_version"


def test_audit_entries_are_api_audit_entries():
    def handler(request):
        body = json.loads(request.content)
        assert body == {"tool": "get_audit_log", "args": {"environment": "Development",
                                                          "features": ["agent_prompt_version"]}}
        return httpx.Response(200, json={"tool": "get_audit_log", "is_error": False, "result": {
            "gate": [GATE_LINE, {"broken": True}], "outside_trustgate": []}})

    entries = _client(handler).audit_entries("Development", ["agent_prompt_version"])
    assert entries == [AuditEntry.model_validate(GATE_LINE)]


def test_gate_errors_raise_with_status():
    with pytest.raises(GateError, match="503"):
        _client(lambda r: httpx.Response(503, json={"detail": "not configured"})) \
            .fingerprint("Development")


def test_unreachable_gate_raises():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(GateError, match="unreachable"):
        _client(handler).fingerprint("Development")


def test_audit_entries_without_environment_asks_for_the_gate_trail_only():
    def handler(request):
        assert json.loads(request.content) == {"tool": "get_audit_log", "args": {}}
        return httpx.Response(200, json={"result": {"gate": [GATE_LINE]}})

    assert len(_client(handler).audit_entries()) == 1
