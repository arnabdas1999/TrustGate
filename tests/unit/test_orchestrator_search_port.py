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
