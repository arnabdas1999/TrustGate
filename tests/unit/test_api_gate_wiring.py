"""The API's gate-facing paths with MOCK=0 (Naman's TODO spots in api/main.py)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from contracts.models import AuditEntry
from orchestrator.gate_client import GateError

GATE_WRITE = AuditEntry(ts=datetime(2026, 11, 26, 18, 0, 5, tzinfo=UTC), actor="executor",
                        action="write", tool="create_environment_feature_version",
                        result="applied", proposal_id="prop_1")


class FakeGate:
    fail = False
    instances = 0

    def __init__(self, base_url):
        self.base_url = base_url
        FakeGate.instances += 1

    def audit_entries(self, environment=None, features=()):
        if FakeGate.fail:
            raise GateError("gate unreachable: ConnectError")
        return [GATE_WRITE]


@pytest.fixture
def live_api(monkeypatch):
    monkeypatch.setattr(api_main, "GateClient", FakeGate)
    monkeypatch.setenv("GATE_URL", "http://gate:8100")
    FakeGate.fail = False
    return TestClient(api_main.create_app(mock=False))


def test_audit_reads_the_gate_trail(live_api):
    rows = live_api.get("/api/audit").json()
    assert [r["action"] for r in rows] == ["write"]
    assert rows[0]["tool"] == "create_environment_feature_version"


def test_audit_polling_reuses_one_gate_client(live_api):
    FakeGate.instances = 0
    for _ in range(3):
        assert live_api.get("/api/audit").status_code == 200
    assert FakeGate.instances == 1


def test_audit_reports_an_unreachable_gate(live_api):
    FakeGate.fail = True
    r = live_api.get("/api/audit")
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_mock_mode_never_calls_the_gate(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("gate called in mock mode")
    monkeypatch.setattr(api_main, "GateClient", boom)
    assert TestClient(api_main.create_app(mock=True)).get("/api/audit").status_code == 200


# --- orchestrator hooks (start investigation, verify after the write) ---------------------

from datetime import timedelta  # noqa: E402

import httpx  # noqa: E402

from contracts.models import (  # noqa: E402
    ConfigSource,
    Domain,
    FlagConfig,
    Identity,
    Incident,
    Investigation,
    InvestigationStatus,
    Proposal,
    ProposalState,
    RepairCandidate,
    RepairKind,
    Timestamps,
    Trace,
    WriteOp,
)


@pytest.fixture
def hooked(monkeypatch):
    calls = {"start": [], "verify": []}
    monkeypatch.setattr(api_main, "start_in_background",
                        lambda db, inv_id: calls["start"].append(inv_id))
    monkeypatch.setattr(api_main, "verify_in_background",
                        lambda db, inv_id: calls["verify"].append(inv_id))
    monkeypatch.setenv("APPROVER_PASSWORD", "pw")
    monkeypatch.setenv("APPROVAL_SIGNING_SECRET", "sign")
    monkeypatch.setenv("GATE_URL", "http://gate:8100")
    app = api_main.create_app(mock=False)
    return TestClient(app), app.state.store, calls


def _seed(store):
    now = datetime.now(UTC)
    identity = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
    flags = FlagConfig(values={}, source=ConfigSource.CAPTURED)
    trace = Trace(run_id="r", domain=Domain.OPSPILOT, case_id="c", flags=flags,
                  identity=identity, model="m")
    store.incidents["inc_1"] = Incident(
        id="inc_1", domain=Domain.OPSPILOT, case_id="c", identity=identity,
        resolved_flags=flags, baseline_snapshot="file:b", release_snapshot="gate:Development",
        db_snapshot="d", trace=trace, created_at=now)
    store.investigations["inv_1"] = Investigation(
        id="inv_1", incident_id="inc_1", domain=Domain.OPSPILOT,
        status=InvestigationStatus.PROPOSED, timestamps=Timestamps(alert=now),
        proposal_id="prop_1")
    op = WriteOp(tool="create_segment_override", project="OpsPilot", environment="Development",
                 feature="agent_prompt_version", segment="tier0_production", value="v1_strict")
    store.proposals["prop_1"] = Proposal(
        id="prop_1", investigation_id="inv_1",
        candidate=RepairCandidate(kind=RepairKind.SEGMENT_OVERRIDE, ops=[op]),
        payload_hash="h", fingerprint="f", explanation="e", state=ProposalState.PROPOSED,
        created_at=now, expires_at=now + timedelta(minutes=15))


def _approve(client):
    token = client.post("/api/session", json={"password": "pw"}).json()["token"]
    return client.post("/api/proposals/prop_1/approve",
                       headers={"Authorization": f"Bearer {token}"})


def test_starting_an_investigation_hands_it_to_the_orchestrator(hooked):
    client, store, calls = hooked
    _seed(store)
    r = client.post("/api/investigations", json={"incident_id": "inc_1"})
    assert r.status_code == 201
    assert calls["start"] == [r.json()["id"]]


def test_applied_write_triggers_the_verifier(hooked, monkeypatch):
    client, store, calls = hooked
    _seed(store)
    monkeypatch.setattr(api_main.httpx, "post",
                        lambda *a, **k: httpx.Response(200, json={"status": "applied"}))
    assert _approve(client).status_code == 200
    assert store.proposals["prop_1"].state is ProposalState.APPLIED
    assert calls["verify"] == ["inv_1"]


def test_stale_write_does_not_trigger_the_verifier(hooked, monkeypatch):
    client, store, calls = hooked
    _seed(store)
    monkeypatch.setattr(api_main.httpx, "post", lambda *a, **k: httpx.Response(409))
    assert _approve(client).status_code == 409
    assert store.investigations["inv_1"].status is InvestigationStatus.STALE
    assert calls["verify"] == []


def test_mock_mode_never_starts_the_orchestrator(monkeypatch):
    started = []
    monkeypatch.setattr(api_main, "start_in_background", lambda *a: started.append(a))
    app = api_main.create_app(mock=True)
    _seed(app.state.store)
    r = TestClient(app).post("/api/investigations", json={"incident_id": "inc_1"})
    assert r.status_code == 201 and started == []
