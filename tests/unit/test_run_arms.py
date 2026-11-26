import json
import sys
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from bench import records
from bench.records import Arm, RunRecord
from bench.run_arms import (
    fetch_view,
    load_global_rollback_evaluator,
    parse_arms,
    record_from_view,
    run_cases,
)
from contracts import (
    ConfigSource,
    Domain,
    FlagConfig,
    Identity,
    Incident,
    InvestigationStatus,
    ProposalState,
    Trace,
)

DEMO_INV = "inv_opspilot_demo"
DEMO_PROP = "prop_opspilot_demo"
CASE = "opspilot-test-1"


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("APPROVER_PASSWORD", "correct horse")
    monkeypatch.setenv("APPROVER_NAME", "arnab")
    return create_app(mock=True)


@pytest.fixture
def client(app):
    return TestClient(app)


def _login(client) -> dict:
    token = client.post("/api/session", json={"password": "correct horse"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _incident(incident_id: str = "inc_test") -> Incident:
    flags = FlagConfig(values={"agent_prompt_version": "v2_fast"}, source=ConfigSource.CAPTURED)
    identity = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
    return Incident(
        id=incident_id, domain=Domain.OPSPILOT, case_id="case_1", identity=identity,
        resolved_flags=flags, baseline_snapshot="baseline_env.json",
        release_snapshot=f"bench/releases/{CASE}.json", db_snapshot="db.sqlite",
        trace=Trace(run_id="run_1", domain=Domain.OPSPILOT, case_id="case_1", flags=flags,
                    identity=identity, model="model_cheap"),
        created_at=datetime(2026, 11, 26, 12, tzinfo=UTC),
    )


@pytest.fixture
def incidents_dir(tmp_path):
    path = tmp_path / "incidents"
    path.mkdir()
    (path / f"{CASE}.json").write_text(_incident().model_dump_json(), encoding="utf-8")
    return path


def _promote_to_proposed(app, incident_id: str) -> None:
    """Stand in for the orchestrator: give the new investigation the demo's proposal."""
    store = app.state.store
    demo_inv, demo_prop = store.investigations[DEMO_INV], store.proposals[DEMO_PROP]
    for inv in store.investigations.values():
        if inv.incident_id != incident_id or inv.status != InvestigationStatus.CAPTURING:
            continue
        prop = demo_prop.model_copy(deep=True, update={"id": f"prop_{inv.id}",
                                                       "investigation_id": inv.id})
        store.proposals[prop.id] = prop
        inv.proposal_id = prop.id
        inv.steps = demo_inv.steps
        inv.culprit_set = demo_inv.culprit_set
        inv.timestamps.first_suspect = inv.timestamps.alert
        inv.timestamps.proposal = datetime.now(UTC)
        inv.status = InvestigationStatus.PROPOSED


def test_record_from_view_on_approved_demo(client):
    approved = client.post(f"/api/proposals/{DEMO_PROP}/approve", headers=_login(client))
    assert approved.status_code == 200
    view = fetch_view(client, DEMO_INV)

    record = record_from_view(CASE, Arm.GUIDED, view, active_ms=4000, approver="arnab")

    ts = view.investigation.timestamps
    assert record.reported_culprit == ["agent_prompt_version"]
    assert record.diagnostic_runs == 22  # search 3+3+3+3, escalate 10
    assert record.cost_kept == 0.94
    assert record.applied_at == ts.verified
    assert record.started_at == ts.alert
    assert record.first_suspect_at == ts.first_suspect
    assert record.validated_repair is True
    assert record.unsolved is False
    assert record.fresh_validation_failed is False
    assert record.flags_reverted == ["agent_prompt_version"]
    assert record.proposal_to_approve_ms is not None and record.proposal_to_approve_ms >= 20_000
    assert record.human_active_ms == 4000
    assert record.inference_usd == 1.84
    assert record.person == "arnab"
    assert record.domain == "opspilot"
    assert record.investigation_id == DEMO_INV


def test_record_from_unapproved_view_is_unsolved(client):
    view = fetch_view(client, DEMO_INV)  # still "proposed"
    record = record_from_view(CASE, "plain", view, active_ms=0, approver=None)
    assert record.unsolved is True
    assert record.applied_at is None
    assert record.validated_repair is False
    assert "timed out" in record.notes


def test_missing_incident_is_skipped(client, tmp_path):
    out, messages = tmp_path / "runs.jsonl", []
    result = run_cases(["opspilot-eval-404"], [Arm.PLAIN], client=client,
                       incidents_dir=tmp_path / "incidents", out=out, emit=messages.append)
    assert result == []
    assert not out.exists()
    assert any("skip opspilot-eval-404" in m and "no captured incident" in m for m in messages)


def test_loop_with_human_approval_writes_record(app, client, incidents_dir, tmp_path):
    fake, out, messages, calls = FakeTime(), tmp_path / "runs.jsonl", [], []

    def sleep(seconds):
        fake.sleep(seconds)
        _promote_to_proposed(app, "inc_test-guided")

    def human(client, investigation_id, *, timeout, poll, sleep, clock):
        before = fetch_view(client, investigation_id)
        calls.append(before.investigation.status)
        response = client.post(f"/api/proposals/{before.proposal.id}/approve",
                               headers=_login(client))
        assert response.status_code == 200
        return fetch_view(client, investigation_id)

    result = run_cases([CASE], [Arm.GUIDED], client=client, incidents_dir=incidents_dir,
                       out=out, sleep=sleep, clock=fake.clock, wait=human,
                       emit=messages.append)

    assert calls == [InvestigationStatus.PROPOSED]
    assert any("create_segment_override agent_prompt_version" in m and "Approve or reject" in m
               for m in messages)
    [record] = list(records.read(out, RunRecord))
    assert result == [record]
    assert record.case == CASE and record.arm == Arm.GUIDED
    assert record.unsolved is False and record.validated_repair is True
    assert record.reported_culprit == ["agent_prompt_version"]
    assert record.person == "arnab"  # read from the audit trail
    inv = app.state.store.investigations[record.investigation_id]
    assert inv.incident_id == "inc_test-guided" and inv.mode == "guided"


def test_timeout_before_proposal_is_unsolved(client, incidents_dir, tmp_path):
    fake, out = FakeTime(), tmp_path / "runs.jsonl"

    def never_called(*args, **kwargs):
        raise AssertionError("no proposal, so no decision to wait for")

    run_cases([CASE], [Arm.PLAIN], client=client, incidents_dir=incidents_dir, out=out,
              timeout=10, poll=2, sleep=fake.sleep, clock=fake.clock, wait=never_called,
              emit=lambda _: None)

    [record] = list(records.read(out, RunRecord))
    assert record.arm == Arm.PLAIN
    assert record.unsolved is True
    assert record.applied_at is None
    assert record.notes == "timed out in status capturing"
    assert sum(fake.sleeps) == 10


def test_default_wait_never_approves(app, client, incidents_dir, tmp_path):
    fake, out = FakeTime(), tmp_path / "runs.jsonl"

    def sleep(seconds):
        fake.sleep(seconds)
        _promote_to_proposed(app, "inc_test-plain")

    [record] = run_cases([CASE], [Arm.PLAIN], client=client, incidents_dir=incidents_dir,
                         out=out, timeout=10, poll=2, sleep=sleep, clock=fake.clock,
                         emit=lambda _: None)

    assert record.unsolved is True
    assert record.notes == "timed out in status proposed"
    inv = app.state.store.investigations[record.investigation_id]
    assert app.state.store.proposals[inv.proposal_id].state == ProposalState.PROPOSED


def test_global_rollback_skipped_without_sandbox(monkeypatch, incidents_dir, tmp_path):
    monkeypatch.setitem(sys.modules, "sandbox", None)  # makes `import sandbox` fail
    assert load_global_rollback_evaluator() is None

    out, messages = tmp_path / "runs.jsonl", []
    result = run_cases([CASE], [Arm.GLOBAL_ROLLBACK], client=None, incidents_dir=incidents_dir,
                       out=out, emit=messages.append)
    assert result == []
    assert not out.exists()
    assert any("needs the sandbox (Khyati)" in m for m in messages)


def test_global_rollback_with_evaluator(incidents_dir, tmp_path):
    releases = tmp_path / "releases"
    releases.mkdir()
    release = {"case": CASE, "changes": [
        {"flag": "agent_prompt_version", "old": "v1_strict", "new": "v2_fast"},
        {"flag": "agent_model", "old": "model_flagship", "new": "model_cheap"}]}
    (releases / f"{CASE}.json").write_text(json.dumps(release), encoding="utf-8")
    seen = []

    def evaluator(incident, rel):
        seen.append((incident.id, rel["case"]))
        return 20, 21, 0.0

    out = tmp_path / "runs.jsonl"
    [record] = run_cases([CASE], [Arm.GLOBAL_ROLLBACK], client=None,
                         incidents_dir=incidents_dir, releases_dir=releases, out=out,
                         load_evaluator=lambda: evaluator, emit=lambda _: None)
    assert seen == [("inc_test", CASE)]
    assert record.arm == Arm.GLOBAL_ROLLBACK
    assert record.flags_reverted == ["agent_prompt_version", "agent_model"]
    assert record.cost_kept == 0.0
    assert record.unsolved is False
    assert "20/21" in record.notes
    assert list(records.read(out, RunRecord)) == [record]


def test_parse_arms():
    assert parse_arms("plain, guided,plain") == [Arm.PLAIN, Arm.GUIDED]
    with pytest.raises(ValueError, match="manual"):
        parse_arms("manual")
    with pytest.raises(ValueError, match="unknown arm"):
        parse_arms("bogus")
