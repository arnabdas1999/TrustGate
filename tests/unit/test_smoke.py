from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from contracts import (
    ConfigSource,
    Domain,
    FlagConfig,
    Identity,
    Incident,
    InvestigationStatus,
    ReadBack,
    Trace,
)
from scripts import smoke

PASSWORD = "correct horse battery"
DEMO_INV = "inv_opspilot_demo"
DEMO_PROP = "prop_opspilot_demo"


class FakeClock:
    """Monotonic clock that only moves when the smoke test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APPROVER_PASSWORD", PASSWORD)
    monkeypatch.setenv("APPROVER_NAME", "arnab")
    return TestClient(create_app(mock=True))


def _incident() -> dict:
    flags = FlagConfig(values={"agent_prompt_version": "v2_fast"}, source=ConfigSource.CAPTURED)
    identity = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    return Incident(
        id="inc_smoke_test", domain=Domain.OPSPILOT, case_id="case_1", identity=identity,
        resolved_flags=flags, baseline_snapshot="baseline.json", release_snapshot="release.json",
        db_snapshot="db.sqlite",
        trace=Trace(run_id="run_1", domain=Domain.OPSPILOT, case_id="case_1", flags=flags,
                    identity=identity, model="model_cheap"),
        created_at=now,
    ).model_dump(mode="json")


def _names(result: smoke.SmokeResult) -> list[str]:
    return [s.name for s in result.steps]


def test_passes_on_mock_demo_investigation(client):
    result = smoke.run_smoke(client, PASSWORD)
    assert result.passed, smoke.format_result(result)
    assert _names(result) == ["health", "find investigation", "session", "approve",
                              "wait for verified", "readback"]
    assert all(s.ok for s in result.steps)
    assert "mock=true" in result.steps[0].detail
    assert DEMO_PROP in result.steps[1].detail


def test_explicit_investigation_id(client):
    result = smoke.run_smoke(client, PASSWORD, investigation_id=DEMO_INV)
    assert result.passed, smoke.format_result(result)


def test_explicit_investigation_not_proposed_fails(client):
    result = smoke.run_smoke(client, PASSWORD, investigation_id="inv_vaultguard_demo")
    assert not result.passed
    assert result.steps[-1].name == "find investigation"
    assert "searching, not proposed" in result.steps[-1].detail


def test_wrong_password_fails_at_session(client):
    result = smoke.run_smoke(client, "not the password")
    assert not result.passed
    last = result.steps[-1]
    assert (last.name, last.ok) == ("session", False)
    assert "401" in last.detail


def test_server_without_password_fails_with_503(monkeypatch):
    monkeypatch.delenv("APPROVER_PASSWORD", raising=False)
    result = smoke.run_smoke(TestClient(create_app(mock=True)), PASSWORD)
    assert result.steps[-1].name == "session"
    assert "503" in result.steps[-1].detail
    assert "not set on the server" in result.steps[-1].detail


def test_no_proposed_investigation_fails_cleanly(client):
    assert smoke.run_smoke(client, PASSWORD).passed  # approves the only proposed demo
    result = smoke.run_smoke(client, PASSWORD)
    assert not result.passed
    last = result.steps[-1]
    assert (last.name, last.ok) == ("find investigation", False)
    assert "no investigation in status proposed" in last.detail


def test_already_approved_proposal_fails_at_approve(client):
    # Find the investigation, then let someone else approve it before the smoke test does.
    token = client.post("/api/session", json={"password": PASSWORD}).json()["token"]
    original_post = client.post

    def post(url, *args, **kwargs):
        if url.endswith("/approve"):
            original_post(url, headers={"Authorization": f"Bearer {token}"})
        return original_post(url, *args, **kwargs)

    client.post = post
    result = smoke.run_smoke(client, PASSWORD)
    assert result.steps[-1].name == "approve"
    assert "409" in result.steps[-1].detail
    assert "verified, not proposed" in result.steps[-1].detail


def test_incident_run_times_out_without_waiting(client):
    clock = FakeClock()
    result = smoke.run_smoke(client, PASSWORD, incident=_incident(), timeout_s=30, poll_s=1.0,
                             sleep=clock.sleep, clock=clock)
    assert not result.passed
    assert _names(result) == ["health", "create incident", "start investigation",
                              "wait for proposal"]
    last = result.steps[-1]
    assert not last.ok
    assert "timed out after 30s" in last.detail
    assert "capturing" in last.detail
    assert clock.sleeps == 30
    assert last.elapsed_ms == 30_000


def test_incident_run_stops_on_terminal_status(client):
    clock = FakeClock()
    store = client.app.state.store

    def sleep(seconds):
        clock.sleep(seconds)
        for inv in store.investigations.values():
            if inv.incident_id == "inc_smoke_test":
                inv.status = InvestigationStatus.NOT_REPRODUCIBLE

    result = smoke.run_smoke(client, PASSWORD, incident=_incident(), sleep=sleep, clock=clock)
    assert result.steps[-1].name == "wait for proposal"
    assert "not_reproducible" in result.steps[-1].detail
    assert clock.sleeps == 1


def test_default_handler_readback_fails(client):
    original_post = client.post

    def post(url, *args, **kwargs):
        response = original_post(url, *args, **kwargs)
        if url.endswith("/approve"):
            proposal = client.app.state.store.proposals[DEMO_PROP]
            proposal.readback.append(ReadBack(identity=Identity(identifier="offline"),
                                              values={}, from_default_handler=True))
        return response

    client.post = post
    result = smoke.run_smoke(client, PASSWORD)
    assert not result.passed
    assert result.steps[-1].name == "readback"
    assert "default handler for: offline" in result.steps[-1].detail


def test_password_never_printed(client, capsys):
    for password in (PASSWORD, "wrong secret value"):
        result = smoke.run_smoke(client, password)
        print(smoke.format_result(result))
    out = capsys.readouterr()
    assert "SMOKE PASSED" in out.out
    assert "SMOKE FAILED" in out.out
    for password in (PASSWORD, "wrong secret value"):
        assert password not in out.out
        assert password not in out.err


def test_main_without_password_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("APPROVER_PASSWORD", raising=False)
    assert smoke.main(["--api", "http://127.0.0.1:9"]) == 2
    assert "APPROVER_PASSWORD" in capsys.readouterr().err


def test_main_unreachable_api_fails_without_leaking(monkeypatch, capsys):
    monkeypatch.setenv("APPROVER_PASSWORD", PASSWORD)
    assert smoke.main(["--api", "http://127.0.0.1:9", "--timeout", "1"]) == 1
    out = capsys.readouterr()
    assert "SMOKE FAILED" in out.out
    assert PASSWORD not in out.out + out.err
