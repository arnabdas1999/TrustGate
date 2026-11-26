import json

import httpx
import pytest

from contracts.models import AuditEntry, WriteOp
from orchestrator.gate_client import GateBusy, GateClient, GateError

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


def test_audit_entries_use_the_unlogged_audit_endpoint():
    def handler(request):
        assert request.method == "GET" and request.url.path == "/gate/audit"
        return httpx.Response(200, json={"gate": [GATE_LINE, {"broken": True}]})

    assert _client(handler).audit_entries() == [AuditEntry.model_validate(GATE_LINE)]


def test_register_returns_the_gates_fingerprint_and_calls():
    def handler(request):
        assert request.url.path == "/gate/register"
        body = json.loads(request.content)
        assert body["proposal_id"] == "prop_1" and len(body["ops"]) == 1
        return httpx.Response(200, json={"proposal_id": "prop_1", "fingerprint": "f" * 64,
                                         "payload_hash": "h" * 64, "calls": [{"tool": "x"}]})

    reg = _client(handler).register(OPS, "prop_1")
    assert reg["fingerprint"] == "f" * 64 and reg["calls"] == [{"tool": "x"}]


def test_shared_token_is_sent_when_configured():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"gate": []})

    client = GateClient("http://gate:8100", token="tok",
                        client=httpx.Client(transport=httpx.MockTransport(handler),
                                            base_url="http://gate:8100"))
    client.audit_entries()
    assert seen == ["Bearer tok"]


def test_gate_errors_raise_with_status():
    with pytest.raises(GateError, match="503"):
        _client(lambda r: httpx.Response(503, json={"detail": "not configured"})) \
            .fingerprint("Development")


def test_503_is_typed_busy_and_other_errors_are_not():
    with pytest.raises(GateBusy) as busy:
        _client(lambda r: httpx.Response(503, json={"detail": "settling"})).register(OPS, "p")
    assert busy.value.status == 503
    with pytest.raises(GateError) as other:
        _client(lambda r: httpx.Response(422, json={"detail": "bad"})).register(OPS, "p")
    assert other.value.status == 422 and not isinstance(other.value, GateBusy)


def test_unreachable_gate_raises():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(GateError, match="unreachable") as exc:
        _client(handler).fingerprint("Development")
    assert exc.value.status is None and not isinstance(exc.value, GateBusy)
