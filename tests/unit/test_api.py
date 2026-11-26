import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from contracts import ProposalState

DEMO_INV = "inv_opspilot_demo"
DEMO_PROP = "prop_opspilot_demo"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APPROVER_PASSWORD", "correct horse")
    monkeypatch.setenv("APPROVER_NAME", "arnab")
    return TestClient(create_app(mock=True))


def _login(client) -> dict:
    token = client.post("/api/session", json={"password": "correct horse"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_health_reports_mock(client):
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "mock": True, "gate_url": body["gate_url"]}


def test_lists_and_gets_investigations(client):
    ids = [i["id"] for i in client.get("/api/investigations").json()]
    assert DEMO_INV in ids
    view = client.get(f"/api/investigations/{DEMO_INV}").json()
    assert view["investigation"]["status"] == "proposed"
    assert view["proposal"]["id"] == DEMO_PROP
    assert len(view["proposal"]["payload_hash"]) == 64


def test_unknown_investigation_is_404(client):
    assert client.get("/api/investigations/nope").status_code == 404


def test_login_rejects_wrong_password(client):
    assert client.post("/api/session", json={"password": "wrong"}).status_code == 401


def test_login_disabled_without_password(monkeypatch):
    monkeypatch.delenv("APPROVER_PASSWORD", raising=False)
    client = TestClient(create_app(mock=True))
    assert client.post("/api/session", json={"password": ""}).status_code == 503


def test_approve_requires_login(client):
    assert client.post(f"/api/proposals/{DEMO_PROP}/approve").status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=bad).status_code == 401


def test_approve_in_mock_mode_verifies_and_is_single_use(client):
    headers = _login(client)
    first = client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=headers)
    assert first.status_code == 200
    assert first.json()["state"] == ProposalState.VERIFIED
    assert {r["identity"]["identifier"] for r in first.json()["readback"]} == {
        "payments-prod", "staging-api"}
    view = client.get(f"/api/investigations/{DEMO_INV}").json()
    assert view["investigation"]["status"] == "verified"
    assert view["investigation"]["timestamps"]["verified"] is not None
    # A second approval of the same proposal is refused.
    assert client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=headers).status_code == 409


def test_reject(client):
    headers = _login(client)
    response = client.post(f"/api/proposals/{DEMO_PROP}/reject", headers=headers)
    assert response.json()["state"] == "rejected"
    assert client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=headers).status_code == 409


def test_audit_records_the_decision(client):
    client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=_login(client))
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert "approve" in actions
    assert "refused" in actions  # mock shows a denied read


def test_activity_totals(client):
    for ms in (1500, 2500):
        response = client.post("/api/activity", json={
            "investigation_id": DEMO_INV, "active_ms": ms, "visible": True,
            "ts": "2026-11-26T12:00:00Z"})
        assert response.status_code == 204
    assert client.get(f"/api/activity/{DEMO_INV}").json()["active_ms"] == 4000
    assert client.get(f"/api/investigations/{DEMO_INV}").json()["active_ms"] == 4000


def test_start_investigation_needs_known_incident(client):
    response = client.post("/api/investigations", json={"incident_id": "missing"})
    assert response.status_code == 404


def test_real_mode_refuses_approval_without_signing_secret(monkeypatch):
    monkeypatch.setenv("APPROVER_PASSWORD", "correct horse")
    monkeypatch.delenv("APPROVAL_SIGNING_SECRET", raising=False)
    app = create_app(mock=False)
    # Seed one proposal from the mock fixtures into the real-mode store.
    from api import mock_data
    app.state.store.investigations, app.state.store.proposals, _ = mock_data.build()
    client = TestClient(app)
    response = client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=_login(client))
    assert response.status_code == 503
