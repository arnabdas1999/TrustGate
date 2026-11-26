from datetime import UTC, datetime

import pytest

from contracts.models import (
    ConfigSource,
    Domain,
    FlagChange,
    FlagConfig,
    Identity,
    Incident,
    InvestigationMode,
    RepairKind,
    TestOutcome,
    TestPhase,
    Trace,
)
from orchestrator.search_port import (
    ScriptedSearch,
    SearchOutcome,
    SearchUnavailable,
    UnavailableSearch,
    search_from_env,
)

SUSPECTS = [FlagChange(flag="agent_context_mode", old="summary", new="full_history"),
            FlagChange(flag="agent_prompt_version", old="v1_strict", new="v2_fast"),
            FlagChange(flag="agent_tool_policy", old="restricted", new="expanded")]


def incident():
    identity = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
    flags = FlagConfig(values={"agent_prompt_version": "v2_fast"}, source=ConfigSource.CAPTURED)
    trace = Trace(run_id="r1", domain=Domain.OPSPILOT, case_id="hero", flags=flags,
                  identity=identity, model="model_cheap")
    return Incident(id="inc_1", domain=Domain.OPSPILOT, case_id="hero", identity=identity,
                    resolved_flags=flags, baseline_snapshot="file:b.json",
                    release_snapshot="gate:Development", db_snapshot="db", trace=trace,
                    created_at=datetime(2026, 11, 26, tzinfo=UTC))


def test_scripted_search_names_the_configured_culprit():
    result = ScriptedSearch(["agent_prompt_version"]).run(incident(), SUSPECTS,
                                                          InvestigationMode.PLAIN)
    assert result.outcome is SearchOutcome.FOUND
    assert [c.flag for c in result.culprit_set] == ["agent_prompt_version"]
    assert result.steps[0].phase is TestPhase.REPRODUCE
    assert result.steps[0].outcome is TestOutcome.FAIL
    assert result.runs_used == sum(s.runs for s in result.steps)


def test_scripted_candidates_are_narrowest_first_and_validated():
    result = ScriptedSearch(["agent_prompt_version"]).run(incident(), SUSPECTS,
                                                          InvestigationMode.GUIDED)
    assert [c.kind for c in result.candidates] == [RepairKind.SEGMENT_OVERRIDE,
                                                   RepairKind.SURGICAL, RepairKind.GLOBAL]
    first = result.candidates[0]
    assert first.validation.outcome is TestOutcome.PASS and first.validation.runs == 20
    assert first.ops == []  # ops are built by code in the proposal builder, never here


def test_scripted_search_is_deterministic():
    a = ScriptedSearch(["agent_prompt_version"]).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    b = ScriptedSearch(["agent_prompt_version"]).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert a.model_dump() == b.model_dump()


def test_culprit_not_in_the_release_is_not_reproducible():
    result = ScriptedSearch(["agent_model"]).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert result.outcome is SearchOutcome.NOT_REPRODUCIBLE
    assert result.candidates == []


def test_unavailable_search_says_so():
    with pytest.raises(SearchUnavailable):
        UnavailableSearch().run(incident(), SUSPECTS, InvestigationMode.PLAIN)


@pytest.mark.parametrize(("value", "kind"), [
    ("scripted:agent_prompt_version", ScriptedSearch),
    ("scripted:agent_tool_policy,agent_prompt_version", ScriptedSearch),
    ("", UnavailableSearch),
])
def test_search_from_env(value, kind):
    assert isinstance(search_from_env({"TRUSTGATE_SEARCH": value}), kind)


def test_scripted_validation_configs_are_shaped_like_search_repair():
    result = ScriptedSearch(["agent_prompt_version"]).run(incident(), SUSPECTS,
                                                          InvestigationMode.PLAIN)
    seg, surgical, global_ = (c.validation.config for c in result.candidates)
    assert seg.segment_overrides == {"tier0_production": {"agent_prompt_version": "v1_strict"}}
    assert seg.values["agent_prompt_version"] == "v2_fast"  # the release, outside the segment
    assert surgical.segment_overrides == {} and surgical.values["agent_prompt_version"] == \
        "v1_strict"
    assert global_.values["agent_tool_policy"] == "restricted"


def test_scripted_without_a_segment_skips_the_segment_candidate():
    result = ScriptedSearch(["agent_prompt_version"], segment=None).run(
        incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert [c.kind for c in result.candidates] == [RepairKind.SURGICAL, RepairKind.GLOBAL]


def test_search_from_env_reads_an_optional_segment():
    engine = search_from_env({"TRUSTGATE_SEARCH": "scripted:agent_prompt_version@high_value_wires"})
    assert engine.culprits == ["agent_prompt_version"] and engine.segment == "high_value_wires"
    assert search_from_env({"TRUSTGATE_SEARCH": "scripted:a"}).segment == "tier0_production"
