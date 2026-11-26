import json
from datetime import UTC, datetime

import pytest

from contracts.models import (
    ConfigSource,
    Domain,
    FlagConfig,
    Identity,
    Incident,
    Investigation,
    InvestigationMode,
    InvestigationStatus,
    ProposalState,
    RepairKind,
    Timestamps,
    Trace,
)
from orchestrator.lifecycle import DomainConfig, Orchestrator
from orchestrator.repo import SqliteRepo
from orchestrator.search_port import ScriptedSearch, UnavailableSearch
from verifier.readback import ServedFlag

NOW = datetime(2026, 11, 26, 19, 0, tzinfo=UTC)
BASELINE = {"agent_model": "model_flagship", "agent_prompt_version": "v1_strict",
            "agent_tool_policy": "restricted", "agent_max_steps": 6,
            "agent_context_mode": "summary", "agent_temperature": "0.0",
            "agent_safe_fallback": False}
RELEASE = {**BASELINE, "agent_model": "model_cheap", "agent_prompt_version": "v2_fast",
           "agent_tool_policy": "expanded", "agent_max_steps": "12",
           "agent_context_mode": "full_history"}
TIER0_RULES = [{"type": "ALL", "conditions": [], "rules": [
    {"type": "ANY", "rules": [], "conditions": [
        {"operator": "EQUAL", "property_": "env", "value": "prod"}]},
    {"type": "ANY", "rules": [], "conditions": [
        {"operator": "EQUAL", "property_": "tier", "value": "0"}]}]}]
MEMBER = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
CONTROL = Identity(identifier="staging-api", traits={"env": "staging", "tier": 2})
CONFIG = {Domain.OPSPILOT: DomainConfig(project="OpsPilot", environment="Development",
                                        control_identity=CONTROL)}


class FakeGate:
    def __init__(self):
        self.fp = "f" * 64
        self.rendered = []

    def read(self, tool, args):
        assert tool == "get_environment_document" and args == {"environment": "Development"}
        return {"environment": "Development", "fingerprint": self.fp, "view": {
            "features": {k: {"enabled": True, "value": v, "multivariate": []}
                         for k, v in RELEASE.items()},
            "segments": {"tier0_production": {"rules": TIER0_RULES, "overrides": {}}}}}

    def fingerprint(self, environment):
        return self.fp

    def render(self, ops, proposal_id):
        self.rendered.append(proposal_id)
        return [{"tool": "create_environment_feature_version", "arguments": {"n": len(ops)}}]


class FakeFlags:
    """Serves what Flagsmith would after the write, or keeps serving the release."""

    def __init__(self, applied=True):
        self.applied = applied

    def identity_flags(self, identity):
        tier0 = identity.traits.get("env") == "prod" and identity.traits.get("tier") == 0
        value = "v1_strict" if (self.applied and tier0) else "v2_fast"
        return {"agent_prompt_version": ServedFlag(value, True, False)}


@pytest.fixture
def setup(tmp_path):
    repo = SqliteRepo(tmp_path / "t.db")
    baseline = tmp_path / "baseline_env.json"
    baseline.write_text(json.dumps({"environment": "Development", "values": BASELINE}))
    flags = FlagConfig(values=RELEASE, source=ConfigSource.CAPTURED)
    trace = Trace(run_id="r", domain=Domain.OPSPILOT, case_id="hero", flags=flags,
                  identity=MEMBER, model="model_cheap")
    repo.put_incident(Incident(id="inc_1", domain=Domain.OPSPILOT, case_id="hero",
                               identity=MEMBER, resolved_flags=flags,
                               baseline_snapshot=f"file:{baseline}",
                               release_snapshot="gate:Development", db_snapshot="db",
                               trace=trace, created_at=NOW))
    repo.put_investigation(Investigation(id="inv_1", incident_id="inc_1",
                                         domain=Domain.OPSPILOT, mode=InvestigationMode.PLAIN,
                                         status=InvestigationStatus.CAPTURING,
                                         timestamps=Timestamps(alert=NOW)))
    gate = FakeGate()
    flagsrc = FakeFlags()

    def make(search=None):
        return Orchestrator(repo=repo, gate=gate, search=search or ScriptedSearch(
            ["agent_prompt_version"]), configs=CONFIG, flag_source=lambda env: flagsrc,
            sleep=lambda s: None, verify_timeout_s=0)
    return repo, gate, flagsrc, make


def test_run_reaches_a_proposal(setup):
    repo, gate, _, make = setup
    make().run("inv_1")
    inv = repo.get_investigation("inv_1")
    assert inv.status is InvestigationStatus.PROPOSED
    assert [s.flag for s in inv.suspects] == ["agent_context_mode", "agent_max_steps",
                                              "agent_model", "agent_prompt_version",
                                              "agent_tool_policy"]
    assert inv.segment == "tier0_production"
    assert [c.flag for c in inv.culprit_set] == ["agent_prompt_version"]
    assert inv.timestamps.first_suspect and inv.timestamps.proposal
    assert inv.runs_used > 0 and inv.steps
    proposal = repo.get_proposal(inv.proposal_id)
    assert proposal.state is ProposalState.PROPOSED
    assert proposal.candidate.kind is RepairKind.SEGMENT_OVERRIDE
    op = proposal.candidate.ops[0]
    assert (op.tool, op.feature, op.segment, op.value, op.environment) == (
        "create_segment_override", "agent_prompt_version", "tier0_production", "v1_strict",
        "Development")
    assert repo.get_calls(proposal.id)[0]["tool"] == "create_environment_feature_version"
    assert "0 failures in 20 runs" in proposal.explanation


def test_not_reproducible(setup):
    repo, _, _, make = setup
    make(ScriptedSearch(["agent_temperature"])).run("inv_1")  # not in the release
    assert repo.get_investigation("inv_1").status is InvestigationStatus.NOT_REPRODUCIBLE


def test_no_search_engine_is_inconclusive_not_a_crash(setup):
    repo, _, _, make = setup
    make(UnavailableSearch()).run("inv_1")
    inv = repo.get_investigation("inv_1")
    assert inv.status is InvestigationStatus.INCONCLUSIVE and inv.proposal_id is None


def _applied(repo):
    inv = repo.get_investigation("inv_1")
    inv.status = InvestigationStatus.APPLIED
    repo.put_investigation(inv)
    p = repo.get_proposal(inv.proposal_id)
    p.state = ProposalState.APPLIED
    repo.put_proposal(p)
    return p


def test_verified_after_the_write(setup):
    repo, _, _, make = setup
    orch = make()
    orch.run("inv_1")
    _applied(repo)
    orch.verify_after_write("inv_1")
    inv = repo.get_investigation("inv_1")
    proposal = repo.get_proposal(inv.proposal_id)
    assert inv.status is InvestigationStatus.VERIFIED and inv.timestamps.verified
    assert proposal.state is ProposalState.VERIFIED
    assert {r.identity.identifier: r.values["agent_prompt_version"]
            for r in proposal.readback} == {"payments-prod": "v1_strict", "staging-api": "v2_fast"}


def test_failed_verification_proposes_a_rollback(setup):
    repo, _, flagsrc, make = setup
    orch = make()
    orch.run("inv_1")
    original = _applied(repo)
    flagsrc.applied = False  # Flagsmith keeps serving the release
    orch.verify_after_write("inv_1")
    inv = repo.get_investigation("inv_1")
    assert inv.status is InvestigationStatus.FAILED
    assert repo.get_proposal(original.id).state is ProposalState.FAILED
    rollback = repo.get_proposal(inv.proposal_id)
    assert rollback.id != original.id and rollback.state is ProposalState.PROPOSED
    assert [(o.tool, o.feature) for o in rollback.candidate.ops] == [
        ("delete_feature_segment", "agent_prompt_version")]
    assert rollback.explanation.startswith("Rollback of")


def test_reinvestigate_after_stale_builds_a_new_proposal(setup):
    repo, gate, _, make = setup
    orch = make()
    orch.run("inv_1")
    first = repo.get_investigation("inv_1").proposal_id
    inv = repo.get_investigation("inv_1")
    inv.status = InvestigationStatus.STALE
    repo.put_investigation(inv)
    gate.fp = "e" * 64
    orch.reinvestigate("inv_1")
    inv = repo.get_investigation("inv_1")
    assert inv.status is InvestigationStatus.PROPOSED and inv.proposal_id != first
    assert repo.get_proposal(inv.proposal_id).fingerprint == "e" * 64


def test_reinvestigate_only_from_stale(setup):
    repo, _, _, make = setup
    orch = make()
    orch.run("inv_1")
    with pytest.raises(ValueError, match="STALE"):
        orch.reinvestigate("inv_1")
