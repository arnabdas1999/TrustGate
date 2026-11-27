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


# --- /gate/register 503 retry ------------------------------------------------------------------

from orchestrator.gate_client import GateBusy, GateError  # noqa: E402


class FlakyGate:
    def __init__(self, failures, error=None):
        self.failures, self.calls = failures, 0
        self.error = error or GateBusy("gate /gate/register returned HTTP 503", status=503)

    def register(self, ops, proposal_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return {"proposal_id": proposal_id, "fingerprint": "f" * 64, "payload_hash": "h",
                "calls": [{"tool": "create_environment_feature_version", "arguments": {}}]}


def _ops():
    return build_ops(RepairKind.SURGICAL, [PROMPT], SUSPECTS, segment=None, project="OpsPilot",
                     environment="Development")


def test_register_503_is_retried_until_the_gate_settles():
    gate, slept = FlakyGate(failures=3), []
    proposal, _ = build_proposal("inv_1", _cand(RepairKind.SURGICAL), _ops(), gate,
                                 environment="Development", explanation="why", now=NOW,
                                 sleep=slept.append)
    assert gate.calls == 4 and slept == [3.0, 3.0, 3.0] and proposal.fingerprint == "f" * 64


def test_register_503_gives_up_after_about_30_seconds():
    clock = [0.0]

    def sleep(s):
        clock[0] += s
    gate = FlakyGate(failures=100)
    with pytest.raises(GateBusy):
        build_proposal("inv_1", _cand(RepairKind.SURGICAL), _ops(), gate,
                       environment="Development", explanation="why", now=NOW, sleep=sleep,
                       monotonic=lambda: clock[0])
    assert clock[0] <= 30.0 and gate.calls <= 11


def test_other_gate_errors_are_not_retried():
    gate = FlakyGate(failures=1, error=GateError("gate returned HTTP 422", status=422))
    with pytest.raises(GateError):
        build_proposal("inv_1", _cand(RepairKind.SURGICAL), _ops(), gate,
                       environment="Development", explanation="why", now=NOW,
                       sleep=lambda s: None)
    assert gate.calls == 1


# --- the built ops must match what the search validated (PR #1 item 5) ------------------------

from orchestrator.proposals import check_ops_match_validation  # noqa: E402

CAPTURED = {"agent_prompt_version": "v2_fast", "agent_tool_policy": "expanded",
            "agent_max_steps": "12"}


def _validated(kind, values=None, overrides=None):
    cfg = FlagConfig(values=values or CAPTURED, source=ConfigSource.OVERRIDE,
                     segment_overrides=overrides or {})
    v = TestResult(phase=TestPhase.VALIDATE, runs=20, failures=0, outcome=TestOutcome.PASS,
                   config=cfg)
    return RepairCandidate(kind=kind, ops=[], validation=v)


def _built(kind, segment="tier0_production"):
    return build_ops(kind, [PROMPT], SUSPECTS, segment=segment, project="OpsPilot",
                     environment="Development")


def test_ops_matching_the_validated_config_pass():
    seg = _validated(RepairKind.SEGMENT_OVERRIDE,
                     overrides={"tier0_production": {"agent_prompt_version": "v1_strict"}})
    check_ops_match_validation(_built(RepairKind.SEGMENT_OVERRIDE), seg, SUSPECTS)
    surgical = _validated(RepairKind.SURGICAL, {**CAPTURED, "agent_prompt_version": "v1_strict"})
    check_ops_match_validation(_built(RepairKind.SURGICAL), surgical, SUSPECTS)
    glob = _validated(RepairKind.GLOBAL, {"agent_prompt_version": "v1_strict",
                                          "agent_tool_policy": "restricted", "agent_max_steps": 6})
    check_ops_match_validation(_built(RepairKind.GLOBAL), glob, SUSPECTS)


@pytest.mark.parametrize("overrides", [
    {"high_value_wires": {"agent_prompt_version": "v1_strict"}},  # another segment
    {"tier0_production": {"agent_prompt_version": "v3"}},  # another value
    {"tier0_production": {"agent_prompt_version": "v1_strict", "agent_tool_policy": "x"}},
    {},
])
def test_segment_ops_that_differ_from_the_validated_override_are_refused(overrides):
    cand = _validated(RepairKind.SEGMENT_OVERRIDE, overrides=overrides)
    with pytest.raises(ProposalError):
        check_ops_match_validation(_built(RepairKind.SEGMENT_OVERRIDE), cand, SUSPECTS)


@pytest.mark.parametrize("values", [
    CAPTURED,  # validated the release itself: nothing was reverted
    {**CAPTURED, "agent_prompt_version": "v0"},  # validated another value
    {**CAPTURED, "agent_prompt_version": "v1_strict", "agent_tool_policy": "restricted"},
])
def test_surgical_ops_that_differ_from_the_validated_values_are_refused(values):
    with pytest.raises(ProposalError):
        check_ops_match_validation(_built(RepairKind.SURGICAL),
                                   _validated(RepairKind.SURGICAL, values), SUSPECTS)


def test_unvalidated_candidate_is_refused():
    with pytest.raises(ProposalError, match="never validated"):
        check_ops_match_validation(_built(RepairKind.SURGICAL),
                                   RepairCandidate(kind=RepairKind.SURGICAL, ops=[]), SUSPECTS)


# --- candidates that carry their own ops (per-flag repairs) ------------------------------------

from contracts.models import WriteOp  # noqa: E402
from orchestrator.proposals import ops_for_candidate, passing_candidates  # noqa: E402

CULPRITS = [PROMPT, TOOLS]


def _op(tool="update_feature_state", feature="agent_tool_policy", value="restricted",
        segment=None, **kw):
    return WriteOp(tool=tool, project=kw.pop("project", "anything"),
                   environment=kw.pop("environment", "OpsPilot Prod"), feature=feature,
                   value=value, segment=segment, **kw)


def _for(kind, ops, segment="tier0_production"):
    cand = RepairCandidate(kind=kind, ops=ops, validation=_validation())
    return ops_for_candidate(cand, CULPRITS, SUSPECTS, segment=segment, project="OpsPilot",
                             environment="Development")


def test_candidate_without_ops_is_built_from_its_kind():
    assert _for(RepairKind.SURGICAL, []) == build_ops(
        RepairKind.SURGICAL, CULPRITS, SUSPECTS, segment=None, project="OpsPilot",
        environment="Development")


def test_per_flag_candidate_keeps_its_flag_and_takes_scope_from_config():
    ops = _for(RepairKind.SURGICAL, [_op()])  # revert the tools only, not the prompt
    assert [(o.tool, o.feature, o.value, o.project, o.environment, o.segment) for o in ops] == [
        ("update_feature_state", "agent_tool_policy", "restricted", "OpsPilot", "Development",
         None)]


def test_per_flag_segment_candidate_uses_the_triaged_segment():
    ops = _for(RepairKind.SEGMENT_OVERRIDE,
               [_op("create_segment_override", "agent_prompt_version", "v1_strict",
                    segment="tier0_production")])
    assert [(o.feature, o.segment, o.value) for o in ops] == [
        ("agent_prompt_version", "tier0_production", "v1_strict")]


@pytest.mark.parametrize("op", [
    _op(tool="create_segment_override", segment="tier0_production"),  # wrong tool for surgical
    _op(feature="agent_max_steps", value=6),  # not a culprit
    _op(value="admin"),  # not the baseline value
    _op(enabled=False),  # never toggles enabled
])
def test_surgical_candidate_ops_the_search_cannot_choose(op):
    with pytest.raises(ProposalError):
        _for(RepairKind.SURGICAL, [op])


def test_duplicate_flags_and_foreign_segments_are_refused():
    with pytest.raises(ProposalError, match="twice"):
        _for(RepairKind.SURGICAL, [_op(), _op()])
    with pytest.raises(ProposalError, match="triaged"):
        _for(RepairKind.SEGMENT_OVERRIDE,
             [_op("create_segment_override", "agent_prompt_version", "v1_strict",
                  segment="high_value_wires")])


def test_global_candidate_ops_may_name_any_release_change():
    ops = _for(RepairKind.GLOBAL, [_op(feature="agent_max_steps", value="6")])
    assert [(o.feature, o.value) for o in ops] == [("agent_max_steps", 6)]  # typed baseline


def test_inconclusive_and_unvalidated_candidates_are_never_proposed():
    inconclusive = TestResult(phase=TestPhase.VALIDATE, runs=20, failures=0,
                              outcome=TestOutcome.INCONCLUSIVE,
                              config=FlagConfig(values={}, source=ConfigSource.OVERRIDE))
    cands = [RepairCandidate(kind=RepairKind.SEGMENT_OVERRIDE, ops=[], validation=inconclusive),
             RepairCandidate(kind=RepairKind.GLOBAL, ops=[]),  # listed, never validated
             _cand(RepairKind.SURGICAL)]
    assert [c.kind for c in passing_candidates(cands)] == [RepairKind.SURGICAL]
