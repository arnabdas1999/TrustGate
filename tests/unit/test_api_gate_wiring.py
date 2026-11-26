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

    def __init__(self, base_url):
        self.base_url = base_url

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


def test_audit_reports_an_unreachable_gate(live_api):
    FakeGate.fail = True
    r = live_api.get("/api/audit")
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_mock_mode_never_calls_the_gate(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("gate called in mock mode")
    monkeypatch.setattr(api_main, "GateClient", boom)
    assert TestClient(api_main.create_app(mock=True)).get("/api/audit").status_code == 200
