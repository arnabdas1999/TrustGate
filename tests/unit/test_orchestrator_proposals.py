from datetime import UTC, datetime, timedelta

import pytest

from contracts.hashing import payload_hash
from contracts.models import (
    ConfigSource,
    FlagChange,
    FlagConfig,
    Identity,
    ProposalState,
    RepairCandidate,
    RepairKind,
    TestOutcome,
    TestPhase,
    TestResult,
)
from orchestrator.proposals import (
    ProposalError,
    build_ops,
    build_proposal,
    choose_candidate,
    expectations,
    inverse_ops,
)

NOW = datetime(2026, 11, 26, 19, 0, tzinfo=UTC)
PROMPT = FlagChange(flag="agent_prompt_version", old="v1_strict", new="v2_fast")
TOOLS = FlagChange(flag="agent_tool_policy", old="restricted", new="expanded")
STEPS = FlagChange(flag="agent_max_steps", old=6, new="12")
SUSPECTS = [STEPS, PROMPT, TOOLS]
TIER0 = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
STAGING = Identity(identifier="staging-api", traits={"env": "staging", "tier": 2})


def _validation(outcome=TestOutcome.PASS, failures=0):
    return TestResult(phase=TestPhase.VALIDATE, runs=20, failures=failures, outcome=outcome,
                      config=FlagConfig(values={}, source=ConfigSource.OVERRIDE))


def _cand(kind, outcome=TestOutcome.PASS, failures=0):
    return RepairCandidate(kind=kind, ops=[], validation=_validation(outcome, failures))


def test_choose_narrowest_passing_candidate():
    cands = [_cand(RepairKind.GLOBAL), _cand(RepairKind.SURGICAL),
             _cand(RepairKind.SEGMENT_OVERRIDE, TestOutcome.FAIL, 1)]
    assert choose_candidate(cands).kind is RepairKind.SURGICAL
    assert choose_candidate([_cand(RepairKind.SEGMENT_OVERRIDE, TestOutcome.INCONCLUSIVE)]) \
        is None
    assert choose_candidate([RepairCandidate(kind=RepairKind.GLOBAL, ops=[])]) is None


def test_segment_override_ops_revert_the_culprit_for_the_segment_only():
    ops = build_ops(RepairKind.SEGMENT_OVERRIDE, [PROMPT], SUSPECTS, segment="tier0_production",
                    project="OpsPilot", environment="Development")
    assert [(o.tool, o.feature, o.segment, o.value) for o in ops] == [
        ("create_segment_override", "agent_prompt_version", "tier0_production", "v1_strict")]
    assert ops[0].project == "OpsPilot" and ops[0].environment == "Development"


def test_surgical_reverts_the_culprit_for_everyone():
    ops = build_ops(RepairKind.SURGICAL, [PROMPT], SUSPECTS, segment="tier0_production",
                    project="OpsPilot", environment="Development")
    assert [(o.tool, o.segment, o.value) for o in ops] == [
        ("update_feature_state", None, "v1_strict")]


def test_global_reverts_every_changed_flag_with_typed_baseline_values():
    ops = build_ops(RepairKind.GLOBAL, [PROMPT], SUSPECTS, segment=None,
                    project="OpsPilot", environment="Development")
    assert [(o.feature, o.value) for o in ops] == [
        ("agent_max_steps", 6), ("agent_prompt_version", "v1_strict"),
        ("agent_tool_policy", "restricted")]


def test_segment_override_needs_a_segment():
    with pytest.raises(ProposalError, match="segment"):
        build_ops(RepairKind.SEGMENT_OVERRIDE, [PROMPT], SUSPECTS, segment=None,
                  project="OpsPilot", environment="Development")


def test_flag_added_by_the_release_cannot_be_reverted_by_value():
    added = FlagChange(flag="new_flag", old=None, new="x")
    with pytest.raises(ProposalError, match="new_flag"):
        build_ops(RepairKind.SURGICAL, [added], [added], segment=None, project="OpsPilot",
                  environment="Development")


def test_inverse_ops_undo_each_op():
    ops = build_ops(RepairKind.SEGMENT_OVERRIDE, [PROMPT], SUSPECTS, segment="tier0_production",
                    project="OpsPilot", environment="Development")
    inv = inverse_ops(ops, {"agent_prompt_version": "v2_fast"})
    assert [(o.tool, o.feature, o.segment, o.value) for o in inv] == [
        ("delete_feature_segment", "agent_prompt_version", "tier0_production", None)]
    surgical = build_ops(RepairKind.SURGICAL, [PROMPT], SUSPECTS, segment=None,
                         project="OpsPilot", environment="Development")
    assert inverse_ops(surgical, {"agent_prompt_version": "v2_fast"})[0].value == "v2_fast"


class FakeGate:
    def __init__(self, fingerprints):
        self.fingerprints = list(fingerprints)
        self.rendered = []

    def fingerprint(self, environment):
        return self.fingerprints.pop(0)

    def render(self, ops, proposal_id):
        self.rendered.append((ops, proposal_id))
        return [{"tool": "create_environment_feature_version", "arguments": {"pid": proposal_id}}]

    def register(self, ops, proposal_id):
        self.rendered.append((ops, proposal_id))
        return {"proposal_id": proposal_id, "fingerprint": self.fingerprints[-1],
                "payload_hash": "ignored", "calls": self.render(ops, proposal_id)[:1]}


def test_build_proposal_binds_hash_fingerprint_expiry_and_exact_call():
    cand = _cand(RepairKind.SEGMENT_OVERRIDE)
    ops = build_ops(cand.kind, [PROMPT], SUSPECTS, segment="tier0_production",
                    project="OpsPilot", environment="Development")
    gate = FakeGate(["f" * 64])  # the gate registers with its own settled fingerprint
    proposal, calls = build_proposal("inv_1", cand, ops, gate, environment="Development",
                                     explanation="why", now=NOW, proposal_id="prop_x",
                                     sleep=lambda s: None)
    assert proposal.id == "prop_x" and proposal.investigation_id == "inv_1"
    assert proposal.candidate.ops == ops
    assert proposal.payload_hash == payload_hash(ops)
    assert proposal.fingerprint == "f" * 64
    assert proposal.expires_at == NOW + timedelta(minutes=15)
    assert proposal.state is ProposalState.PROPOSED
    assert calls[0]["arguments"]["pid"] == "prop_x"
    assert gate.rendered[0][1] == "prop_x"


def test_expectations_for_a_segment_override():
    ops = build_ops(RepairKind.SEGMENT_OVERRIDE, [PROMPT], SUSPECTS, segment="tier0_production",
                    project="OpsPilot", environment="Development")
    exp = expectations(ops, TIER0, STAGING, {"agent_prompt_version": "v2_fast"})
    assert [(e.identity.identifier, e.values) for e in exp] == [
        ("payments-prod", {"agent_prompt_version": "v1_strict"}),
        ("staging-api", {"agent_prompt_version": "v2_fast"})]


def test_expectations_for_a_surgical_revert():
    ops = build_ops(RepairKind.SURGICAL, [PROMPT], SUSPECTS, segment=None,
                    project="OpsPilot", environment="Development")
    exp = expectations(ops, TIER0, STAGING, {"agent_prompt_version": "v2_fast"})
    assert [e.values for e in exp] == [{"agent_prompt_version": "v1_strict"}] * 2


def test_expectations_after_deleting_an_override():
    delete = inverse_ops(build_ops(RepairKind.SEGMENT_OVERRIDE, [PROMPT], SUSPECTS,
                                   segment="tier0_production", project="OpsPilot",
                                   environment="Development"),
                         {"agent_prompt_version": "v2_fast"})
    exp = expectations(delete, TIER0, STAGING, {"agent_prompt_version": "v2_fast"})
    assert [e.values for e in exp] == [{"agent_prompt_version": "v2_fast"}] * 2
