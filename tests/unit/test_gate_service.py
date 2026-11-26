from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from contracts.hashing import payload_hash, sign_approval
from contracts.models import Approval, WriteOp
from gate.approvals import ApprovalStore
from gate.audit_log import AuditLog
from gate.mcp_client import ToolResult
from gate.scope import GateScope
from gate.service import GateDeps, GateSettings, create_app
from gate.writer import V2_WRITE_TOOL, StaticIds

SECRET = "svc-secret"
SCOPE = GateScope(project_id=56664, project_name="OpsPilot", environments={"Development": 121197})
IDS = StaticIds(features={"agent_prompt_version": 306127}, segments={"tier0_production": 1205876})
FP = "f" * 64
DOC = {"id": 121197, "api_key": "client-key", "name": "Development", "feature_states": [
    {"enabled": True, "feature": {"id": 1, "name": "agent_prompt_version"},
     "feature_state_value": "v1_strict", "multivariate_feature_state_values": []}],
    "project": {"segments": []}}


class FakeMcp:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments, *, is_write):
        self.calls.append((name, arguments, is_write))
        if name == "get_environment_feature_versions":
            return ToolResult(False, [{"type": "text", "text":
                '{"results": [{"uuid": "v-1", "live_from": "t", "description": "x"}]}'}])
        return ToolResult(False, [{"type": "text", "text": '{"uuid": "v-9", "results": []}'}])


@pytest.fixture
def setup(tmp_path):
    mcp = FakeMcp()
    fps = {"value": FP}
    deps = GateDeps(
        scope=SCOPE, mcp=mcp, ids=IDS,
        approvals=ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        fingerprint=lambda env: fps["value"],
        document=lambda env: DOC, settle_reads=1, sleep=lambda s: None)
    return TestClient(create_app(deps)), mcp, fps, deps


def _ops():
    return [WriteOp(tool="create_segment_override", project="OpsPilot", environment="Development",
                    feature="agent_prompt_version", segment="tier0_production", value="v1_strict")]


def _register(client, ops=None, proposal_id="prop_1"):
    r = client.post("/gate/register", json={"proposal_id": proposal_id,
                                            "ops": [o.model_dump(mode="json")
                                                    for o in (ops or _ops())]})
    assert r.status_code == 200, r.text
    return r.json()


def _write_body(ops=None, secret=SECRET, fingerprint=FP):
    ops = ops or _ops()
    exp = datetime.now(UTC) + timedelta(minutes=10)
    ph = payload_hash(ops)
    approval = Approval(proposal_id="prop_1", approval_id="appr_1", approver="naman",
                        payload_hash=ph, created_at=datetime.now(UTC), expires_at=exp,
                        signature=sign_approval(secret, "appr_1", "prop_1", ph, exp.isoformat()))
    # Exactly the shape api.main._send_to_gate posts.
    return {"approval": approval.model_dump(mode="json"),
            "ops": [o.model_dump(mode="json") for o in ops], "fingerprint": fingerprint}


def test_health(setup):
    client = setup[0]
    assert client.get("/health").json()["mode"] == "live"


def test_unconfigured_gate_starts_and_answers_503():
    client = TestClient(create_app(settings=GateSettings.from_env({})))
    assert client.get("/health").json()["mode"] == "unconfigured"
    assert client.post("/gate/read", json={"tool": "get_feature_flag", "args": {}}).status_code \
        == 503


def test_allowlisted_read_goes_to_mcp_as_a_read(setup):
    client, mcp, _, deps = setup
    r = client.post("/gate/read", json={"tool": "list_project_features",
                                        "args": {"project_pk": 56664}})
    assert r.status_code == 200
    assert mcp.calls[-1] == ("list_project_features", {"project_pk": 56664}, False)
    assert deps.audit.entries()[-1]["action"] == "read"


def test_denied_read_is_403_and_audited(setup):
    client, mcp, _, deps = setup
    r = client.post("/gate/read", json={"tool": "list_organizations", "args": {}})
    assert r.status_code == 403
    assert mcp.calls == []
    assert deps.audit.entries()[-1]["action"] == "refused"


def test_write_tool_through_read_is_403(setup):
    client, mcp, _, _ = setup
    r = client.post("/gate/read", json={"tool": V2_WRITE_TOOL, "args": {}})
    assert r.status_code == 403 and mcp.calls == []


def test_environment_document_read_hides_keys(setup):
    client = setup[0]
    r = client.post("/gate/read", json={"tool": "get_environment_document",
                                        "args": {"environment": "Development"}})
    assert r.status_code == 200
    body = r.json()
    assert "client-key" not in r.text
    assert body["result"]["fingerprint"] and "features" in body["result"]["view"]


def test_audit_log_read_merges_versions(setup):
    client = setup[0]
    _register(client)
    client.post("/gate/write", json=_write_body())
    r = client.post("/gate/read", json={"tool": "get_audit_log", "args": {
        "environment": "Development", "features": ["agent_prompt_version"]}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert any(e["action"] == "write" for e in result["gate"])
    assert "outside_trustgate" in result


def test_write_with_valid_approval(setup):
    client, mcp, _, _ = setup
    _register(client)
    r = client.post("/gate/write", json=_write_body())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    assert [c for c in mcp.calls if c[2]] == [(V2_WRITE_TOOL, r.json()["calls"][0]["arguments"],
                                              True)]


def test_forged_write_is_403(setup):
    client, mcp, _, _ = setup
    _register(client)
    r = client.post("/gate/write", json=_write_body(secret="nope"))
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "forged"
    assert not [c for c in mcp.calls if c[2]]


def test_reused_write_is_refused(setup):
    client = setup[0]
    _register(client)
    body = _write_body()
    assert client.post("/gate/write", json=body).status_code == 200
    r = client.post("/gate/write", json=body)
    assert r.status_code in (403, 409)  # written-over (STALE) or REUSED: never a second write


def test_stale_write_is_409(setup):
    client, mcp, fps, _ = setup
    _register(client)
    fps["value"] = "0" * 64
    r = client.post("/gate/write", json=_write_body())
    assert r.status_code == 409
    assert not [c for c in mcp.calls if c[2]]


def test_fingerprint_endpoint(setup):
    client = setup[0]
    assert client.get("/gate/fingerprint", params={"environment": "Development"}).json() == {
        "environment": "Development", "fingerprint": FP}
    assert client.get("/gate/fingerprint", params={"environment": "Production"}).status_code \
        == 403


def test_render_endpoint_shows_the_exact_call(setup):
    client = setup[0]
    r = client.post("/gate/render", json={"proposal_id": "prop_1",
                                          "ops": [o.model_dump(mode="json") for o in _ops()]})
    assert r.status_code == 200
    call = r.json()["calls"][0]
    assert call["tool"] == V2_WRITE_TOOL
    assert call["arguments"]["description"] == "trustgate proposal=prop_1 approval=<pending>"


def test_settings_parse_environments_and_server_keys():
    s = GateSettings.from_env({
        "FLAGSMITH_API_KEY": "pre.fix", "APPROVAL_SIGNING_SECRET": "s",
        "GATE_PROJECT_ID": "56664", "GATE_PROJECT_NAME": "OpsPilot",
        "GATE_ENVIRONMENTS": "Development:121197, Demo Production:121198",
        "GATE_SERVER_KEY_DEVELOPMENT": "ser.dev", "GATE_SERVER_KEY_DEMO_PRODUCTION": "ser.prod"})
    assert s.configured
    assert s.scope.environments == {"Development": 121197, "Demo Production": 121198}
    assert s.server_keys == {"Development": "ser.dev", "Demo Production": "ser.prod"}
    assert "ser.dev" not in repr(s) and "pre.fix" not in repr(s)


def test_settings_missing_values_are_named():
    s = GateSettings.from_env({"GATE_PROJECT_ID": "56664"})
    assert not s.configured
    assert "FLAGSMITH_API_KEY" in s.missing and "APPROVAL_SIGNING_SECRET" in s.missing


def test_register_records_the_gates_own_fingerprint(setup):
    client, _, fps, deps = setup
    body = _register(client)
    assert body["fingerprint"] == FP and body["calls"][0]["tool"] == V2_WRITE_TOOL
    assert deps.approvals.registration("prop_1").fingerprint == FP


def test_register_conflict_is_409(setup):
    client = setup[0]
    _register(client)
    other = [WriteOp(tool="create_segment_override", project="OpsPilot",
                     environment="Development", feature="agent_prompt_version",
                     segment="tier0_production", value="v2_fast")]
    r = client.post("/gate/register", json={"proposal_id": "prop_1",
                                            "ops": [o.model_dump(mode="json") for o in other]})
    assert r.status_code == 409


def test_write_without_registration_is_refused(setup):
    client, mcp, _, _ = setup
    r = client.post("/gate/write", json=_write_body())
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "bad_request"
    assert not [c for c in mcp.calls if c[2]]


def test_caller_cannot_skip_the_stale_check_with_the_current_fingerprint(setup):
    client, mcp, fps, _ = setup
    _register(client)
    fps["value"] = "0" * 64  # Flagsmith changed; the caller sends the new state
    r = client.post("/gate/write", json=_write_body(fingerprint="0" * 64))
    assert r.status_code in (403, 409)
    assert not [c for c in mcp.calls if c[2]]


def test_gate_audit_endpoint_does_not_log_itself(setup):
    client, _, _, deps = setup
    client.post("/gate/read", json={"tool": "list_project_features",
                                    "args": {"project_pk": 56664}})
    before = len(deps.audit.entries())
    for _ in range(3):
        r = client.get("/gate/audit")
        assert r.status_code == 200 and len(r.json()["gate"]) == before
    assert len(deps.audit.entries()) == before


def test_shared_token_guards_every_gate_endpoint(tmp_path):
    deps = GateDeps(scope=SCOPE, mcp=FakeMcp(), ids=IDS,
                    approvals=ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE),
                    audit=AuditLog(tmp_path / "audit.jsonl"), fingerprint=lambda env: FP,
                    document=lambda env: DOC, settle_reads=1, sleep=lambda s: None)
    client = TestClient(create_app(deps, shared_token="s3cret-token"))
    assert client.get("/health").status_code == 200
    assert client.get("/gate/audit").status_code == 401
    assert client.get("/gate/audit", headers={"Authorization": "Bearer wrong"}).status_code \
        == 401
    assert client.get("/gate/audit",
                      headers={"Authorization": "Bearer s3cret-token"}).status_code == 200


def test_non_numeric_project_id_leaves_the_gate_unconfigured():
    s = GateSettings.from_env({"FLAGSMITH_API_KEY": "pre.fix", "APPROVAL_SIGNING_SECRET": "s",
                               "GATE_PROJECT_ID": "OpsPilot", "GATE_ENVIRONMENTS": "Dev:1",
                               "GATE_SERVER_KEY_DEV": "ser.x"})
    assert not s.configured and "GATE_PROJECT_ID" in s.missing


# --- second review --------------------------------------------------------------------------


def _deps(tmp_path, **kw):
    base = dict(scope=SCOPE, mcp=FakeMcp(), ids=IDS,
                approvals=ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE),
                audit=AuditLog(tmp_path / "audit.jsonl"), fingerprint=lambda env: FP,
                document=lambda env: DOC, settle_reads=1, sleep=lambda s: None)
    base.update(kw)
    return GateDeps(**base)


def test_health_shows_the_auth_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("GATE_SHARED_TOKEN", raising=False)
    assert TestClient(create_app(_deps(tmp_path))).get("/health").json()["auth"] == "off"
    assert TestClient(create_app(_deps(tmp_path), shared_token="t")).get(
        "/health").json()["auth"] == "token"


def test_set_but_empty_token_is_a_config_error_not_open_access(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_SHARED_TOKEN", "  ")
    client = TestClient(create_app(_deps(tmp_path)))
    assert client.get("/health").json()["auth"] == "misconfigured"
    assert client.get("/gate/audit").status_code == 503


def test_register_conflict_is_409_and_audited(setup):
    client, _, _, deps = setup
    _register(client)
    other = [WriteOp(tool="create_segment_override", project="OpsPilot",
                     environment="Development", feature="agent_prompt_version",
                     segment="tier0_production", value="v2_fast")]
    r = client.post("/gate/register", json={"proposal_id": "prop_1",
                                            "ops": [o.model_dump(mode="json") for o in other]})
    assert r.status_code == 409
    last = deps.audit.entries()[-1]
    assert last["action"] == "refused" and last["tool"] == "register_proposal"


def test_other_value_errors_in_register_are_not_reported_as_conflicts(tmp_path, monkeypatch):
    deps = _deps(tmp_path)

    def broken(*a, **k):
        raise ValueError("something else")

    monkeypatch.setattr(deps.approvals, "register_proposal", broken)
    r = TestClient(create_app(deps, shared_token="")).post(
        "/gate/register", json={"proposal_id": "p", "ops": [o.model_dump(mode="json")
                                                            for o in _ops()]})
    assert r.status_code == 422


def test_register_settle_has_a_time_budget(tmp_path):
    clock = {"t": 0.0}
    n = {"i": 0}

    def drifting(env):  # never two equal reads in a row, and each read takes 3 s
        clock["t"] += 3.0
        n["i"] += 1
        return f"{n['i']:064d}"

    deps = _deps(tmp_path, fingerprint=drifting,
                 sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                 monotonic=lambda: clock["t"])
    r = TestClient(create_app(deps, shared_token="")).post(
        "/gate/register", json={"proposal_id": "p", "ops": [o.model_dump(mode="json")
                                                            for o in _ops()]})
    assert r.status_code == 503 and clock["t"] <= 10.0 + 3.0 + 1.0


def test_gate_documents_use_a_short_edge_timeout(monkeypatch, tmp_path):
    import gate.service as service

    seen = {}

    def fake_fetch(key, **kw):
        seen.update(kw)
        return {"id": 121197, "feature_states": []}

    monkeypatch.setattr(service, "fetch_environment_document", fake_fetch)
    s = GateSettings.from_env({"FLAGSMITH_API_KEY": "pre.fix", "APPROVAL_SIGNING_SECRET": "s",
                               "GATE_PROJECT_ID": "56664",
                               "GATE_ENVIRONMENTS": "Development:121197",
                               "GATE_SERVER_KEY_DEVELOPMENT": "ser.x",
                               "GATE_DB_PATH": str(tmp_path / "a.db"),
                               "GATE_AUDIT_PATH": str(tmp_path / "audit.jsonl")})
    GateDeps.from_settings(s).document("Development")
    assert seen.get("timeout", 99) <= 5


def test_filtered_audit_log_still_recognises_our_other_writes(setup):
    client, _, _, deps = setup
    deps.audit.append(actor="executor", action="write", tool="x", args={}, result="applied",
                      proposal_id="prop_other", version_uuids=["v-1"])
    r = client.post("/gate/read", json={"tool": "get_audit_log", "args": {
        "proposal_id": "prop_1", "environment": "Development",
        "features": ["agent_prompt_version"]}})
    assert r.json()["result"]["outside_trustgate"] == []


def test_register_waits_until_the_edge_moves_past_a_written_over_state(tmp_path):
    reads = iter(["a" * 64, "a" * 64, "b" * 64, "b" * 64])
    deps = _deps(tmp_path, fingerprint=lambda env: next(reads))
    deps.approvals.mark_written_over("Development", "a" * 64)  # the edge still lags
    body = TestClient(create_app(deps, shared_token="")).post(
        "/gate/register", json={"proposal_id": "p", "ops": [o.model_dump(mode="json")
                                                            for o in _ops()]}).json()
    assert body["fingerprint"] == "b" * 64


def test_register_gives_up_while_the_edge_still_shows_the_written_over_state(tmp_path):
    clock = {"t": 0.0}
    deps = _deps(tmp_path, fingerprint=lambda env: "a" * 64,
                 sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                 monotonic=lambda: clock["t"])
    deps.approvals.mark_written_over("Development", "a" * 64)
    r = TestClient(create_app(deps, shared_token="")).post(
        "/gate/register", json={"proposal_id": "p", "ops": [o.model_dump(mode="json")
                                                            for o in _ops()]})
    assert r.status_code == 503 and "last write" in r.json()["detail"]
