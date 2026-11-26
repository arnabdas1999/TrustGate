"""Approval path fixes from the PR #1 review: in-flight guard, gate token, timeouts, retries."""

from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api import mock_data
from contracts import AuditEntry, ProposalState, verify_signature

PROP = "prop_opspilot_demo"
INV = "inv_opspilot_demo"


@pytest.fixture
def real_api(monkeypatch):
    monkeypatch.setenv("APPROVER_PASSWORD", "correct horse")
    monkeypatch.setenv("APPROVAL_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.delenv("GATE_SHARED_TOKEN", raising=False)
    verified = []
    monkeypatch.setattr(api_main, "verify_in_background",
                        lambda db, inv_id: verified.append(inv_id))
    app = api_main.create_app(mock=False)
    store = app.state.store
    store.investigations, store.proposals, _ = mock_data.build()
    client = TestClient(app)
    token = client.post("/api/session", json={"password": "correct horse"}).json()["token"]
    return client, store, {"Authorization": f"Bearer {token}"}, verified


def _gate_returns(monkeypatch, response=None, raises=None):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr(api_main.httpx, "post", fake_post)
    return calls


def test_applied_write_sends_the_gate_token_when_configured(real_api, monkeypatch):
    client, store, headers, verified = real_api
    monkeypatch.setenv("GATE_SHARED_TOKEN", "gate-token-123")
    calls = _gate_returns(monkeypatch, httpx.Response(200, json={"status": "applied"}))
    response = client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert response.status_code == 200
    assert response.json()["state"] == "applied"
    assert calls[0]["headers"] == {"Authorization": "Bearer gate-token-123"}
    assert verified == [INV]


def test_no_token_header_when_token_is_off(real_api, monkeypatch):
    client, _, headers, _ = real_api
    calls = _gate_returns(monkeypatch, httpx.Response(200, json={"status": "applied"}))
    client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert calls[0]["headers"] == {}


def test_refused_write_returns_the_proposal_for_another_try(real_api, monkeypatch):
    client, store, headers, verified = real_api
    _gate_returns(monkeypatch, httpx.Response(403, text="refused: scope"))
    assert client.post(f"/api/proposals/{PROP}/approve", headers=headers).status_code == 502
    assert store.proposals[PROP].state == "proposed"
    assert verified == []


def test_in_flight_proposal_refuses_a_second_approve(real_api, monkeypatch):
    client, store, headers, _ = real_api
    calls = _gate_returns(monkeypatch, httpx.Response(200, json={"status": "applied"}))
    store.proposals[PROP].state = ProposalState.APPROVED  # first click still waiting on the gate
    response = client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert response.status_code == 409
    assert calls == []  # the second click never reached the gate


def test_stale_write_is_not_reverted(real_api, monkeypatch):
    client, store, headers, verified = real_api
    _gate_returns(monkeypatch, httpx.Response(409))
    assert client.post(f"/api/proposals/{PROP}/approve", headers=headers).status_code == 409
    assert store.proposals[PROP].state == "stale"
    assert store.investigations[INV].status == "stale"
    assert verified == []


class _AuditGate:
    entries: list[AuditEntry] = []

    def __init__(self, *_a, **_k):
        pass

    def audit_entries(self, proposal_id=None):
        return [e for e in self.entries if proposal_id in (None, e.proposal_id)]


def _write_entry(result):
    return AuditEntry(ts=datetime.now(UTC), actor="executor", action="write",
                      tool="create_environment_feature_version", proposal_id=PROP, result=result)


def test_timeout_after_an_applied_write_is_reported_as_applied(real_api, monkeypatch):
    client, store, headers, verified = real_api
    _gate_returns(monkeypatch, raises=httpx.ReadTimeout("slow gate"))
    _AuditGate.entries = [_write_entry("applied")]
    monkeypatch.setattr(api_main, "GateClient", _AuditGate)
    response = client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert response.status_code == 200
    assert store.proposals[PROP].state == "applied"
    assert verified == [INV]


def test_timeout_with_unknown_outcome_is_504_and_retryable(real_api, monkeypatch):
    client, store, headers, verified = real_api
    _gate_returns(monkeypatch, raises=httpx.ReadTimeout("slow gate"))
    _AuditGate.entries = []
    monkeypatch.setattr(api_main, "GateClient", _AuditGate)
    response = client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert response.status_code == 504
    assert "unknown" in response.json()["detail"]
    assert store.proposals[PROP].state == "proposed"
    assert verified == []


def test_timeout_with_failed_write_says_so(real_api, monkeypatch):
    client, _, headers, _ = real_api
    _gate_returns(monkeypatch, raises=httpx.ReadTimeout("slow gate"))
    _AuditGate.entries = [_write_entry("failed: MCP error")]
    monkeypatch.setattr(api_main, "GateClient", _AuditGate)
    response = client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert response.status_code == 504
    assert "failed" in response.json()["detail"]


def test_approving_a_rollback_keeps_the_first_approval_time(real_api, monkeypatch):
    client, store, headers, _ = real_api
    first = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    store.investigations[INV].timestamps.approved = first
    _gate_returns(monkeypatch, httpx.Response(200, json={"status": "applied"}))
    client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    assert store.investigations[INV].timestamps.approved == first


def test_approval_expiry_leaves_a_margin_under_the_gates_15_minutes(real_api, monkeypatch):
    client, store, headers, _ = real_api
    calls = _gate_returns(monkeypatch, httpx.Response(200, json={"status": "applied"}))
    client.post(f"/api/proposals/{PROP}/approve", headers=headers)
    approval = calls[0]["json"]["approval"]
    created = datetime.fromisoformat(approval["created_at"])
    expires = datetime.fromisoformat(approval["expires_at"])
    assert (expires - created).total_seconds() <= 14 * 60


def test_non_ascii_signature_is_a_refusal_not_a_crash():
    assert verify_signature("secret", "é" * 64, "a", "p", "h", "t") is False
