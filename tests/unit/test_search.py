"""search/: ddmin on synthetic oracles, tiered testing, repair validation, the engine end to end.

No credits: oracles are synthetic, and the end-to-end test uses the scripted OpsPilot agent.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from contracts.models import (
    FlagChange,
    FlagConfig,
    Incident,
    InvestigationMode,
    RepairKind,
    TestOutcome,
    TestPhase,
)
from orchestrator.proposals import choose_candidate
from orchestrator.search_port import SearchOutcome
from sandbox import evaluate_global_rollback
from sandbox.flags import BASELINE, RELEASE, RELEASE_CHANGES, release_config
from search import SandboxSearch
from search.budget import Budget
from search.ddmin import ddmin
from search.engine import SandboxOracle, apply_subset, incident_segment
from search.repair import cost_kept
from search.testing import Observation, Tester, evidence_line, rule_of_three
from tests.unit.sandbox_helpers import adapter, by_prompt

F, P, U = TestOutcome.FAIL, TestOutcome.PASS, TestOutcome.INCONCLUSIVE


# -- ddmin -----------------------------------------------------------------------------------------


def a_or_bc(subset):
    s = set(subset)
    return F if "A" in s or {"B", "C"} <= s else P


def test_ddmin_single_culprit():
    for culprit in "ABCDE":
        dd = ddmin(list("ABCDE"), lambda s, c=culprit: F if c in s else P)
        assert dd.culprit == [culprit] and dd.outcome is F


def test_ddmin_pair_interaction():
    dd = ddmin(list("BDCEFG"), lambda s: F if {"B", "C"} <= set(s) else P)
    assert sorted(dd.culprit) == ["B", "C"]


def test_ddmin_a_or_bc_is_one_minimal():
    for order in (list("ABCDE"), list("EDCBA"), list("BCADE")):
        dd = ddmin(order, a_or_bc)
        assert dd.culprit in (["A"], ["B", "C"], ["C", "B"])
        for drop in dd.culprit:  # removing any one change makes it pass
            assert a_or_bc([c for c in dd.culprit if c != drop]) is P


def test_ddmin_order_matters_and_memoises():
    calls = []
    def oracle(s):
        calls.append(frozenset(s))
        return a_or_bc(s)
    ddmin(list("ABCDE"), oracle)
    assert len(calls) == len(set(calls))  # no subset tested twice


def test_ddmin_fewer_tests_when_culprit_first():
    first = ddmin(["X", "b", "c", "d", "e", "f", "g", "h"], lambda s: F if "X" in s else P)
    last = ddmin(["b", "c", "d", "e", "f", "g", "h", "X"], lambda s: F if "X" in s else P)
    assert first.culprit == last.culprit == ["X"] and first.tests <= last.tests


def test_ddmin_inconclusive_is_reported():
    dd = ddmin(list("ABCD"), lambda s: U if s == ["A", "B"] else (F if {"A", "C"} <= set(s)
                                                                 else P))
    assert dd.outcome is U


# -- tiered testing --------------------------------------------------------------------------------


def oracle_from(fails, *, rate=1.0, rng=None, cost=0.01, error_every=0):
    """fails(values) -> bool. rate < 1 makes the failure flaky."""
    rng = rng or random.Random(0)  # noqa: S311
    state = {"n": 0}

    def oracle(config: FlagConfig, n: int, *, early_stop: bool, case_id=None) -> Observation:
        runs = failures = errors = 0
        for _ in range(n):
            state["n"] += 1
            if error_every and state["n"] % error_every == 0:
                errors += 1
                continue
            runs += 1
            if fails(config.values, case_id) and rng.random() < rate:
                failures += 1
                if early_stop:
                    break
        return Observation(runs=runs, failures=failures, errors=errors,
                           passes=runs - failures, cost_usd=cost * (runs + errors))
    return oracle


def cfg(**values):
    return FlagConfig(values=values, source="override")


def test_tester_phases_and_outcomes():
    t = Tester(oracle_from(lambda v, c: v.get("x") == 1), Budget(100, 10))
    fail = t.test(TestPhase.SEARCH, cfg(x=1), [])
    assert fail.outcome is F and fail.runs == 1  # early stop
    assert t.search_test(cfg(x=0), []) is P  # provisional pass for ddmin...
    ok = t.steps[-1]
    assert ok.outcome is U and ok.runs == 3  # ...recorded honestly as INCONCLUSIVE
    escalated = t.escalate(ok)
    assert escalated.outcome is P and escalated.runs == 10
    full = t.test(TestPhase.REPRODUCE, cfg(x=1), [])
    assert full.runs == 10 and full.failures == 10  # no early stop when counting


def test_tester_tops_up_errored_runs():
    t = Tester(oracle_from(lambda v, c: False, error_every=4), Budget(100, 10))
    r = t.test(TestPhase.CONFIRM, cfg(), [])
    assert r.outcome is P and r.runs == 10
    assert t.budget.runs > 10  # errored runs are still charged


def test_tester_budget_gone_is_inconclusive():
    t = Tester(oracle_from(lambda v, c: False), Budget(max_runs=5))
    assert t.test(TestPhase.CONFIRM, cfg(), []).outcome is U


def test_flaky_failure_can_hide_in_three_runs():
    # 30% failure rate: three clean runs happen ~34% of the time, so search results
    # are only provisional and the escalation step exists.
    rng = random.Random(4)  # noqa: S311
    t = Tester(oracle_from(lambda v, c: True, rate=0.3, rng=rng), Budget(1000, 100))
    outcomes = [t.search_test(cfg(), []) for _ in range(200)]
    clean = outcomes.count(P) / len(outcomes)
    assert 0.2 < clean < 0.5


def test_evidence_numbers():
    assert rule_of_three(20) == 0.15
    assert evidence_line(0, 20).startswith("0 failures in 20 runs. With 95% confidence")
    assert evidence_line(3, 10) == "3 failures in 10 runs"


def test_cost_kept():
    assert cost_kept(1.0, 0.2, 0.2) == 1.0
    assert cost_kept(1.0, 0.2, 1.0) == 0.0
    assert cost_kept(1.0, 0.2, 0.6) == 0.5
    assert cost_kept(1.0, 1.0, 0.5) is None


# -- the engine on synthetic oracles ---------------------------------------------------------------

SUSPECTS = [FlagChange(flag=f, old=BASELINE[f], new=v) for f, v in RELEASE_CHANGES.items()]
TIER0 = {"identifier": "payments-prod", "traits": {"env": "prod", "tier": 0}}


def incident(values=RELEASE, traits=None):
    trace = {"run_id": "r", "domain": "opspilot", "case_id": "hero",
             "flags": {"values": values, "source": "captured"},
             "identity": {"identifier": "payments-prod", "traits": traits or TIER0["traits"]},
             "model": "m"}
    return Incident(id="i1", domain="opspilot", case_id="hero",
                    identity={"identifier": "payments-prod",
                              "traits": traits or TIER0["traits"]},
                    resolved_flags={"values": values, "source": "captured"},
                    baseline_snapshot="b", release_snapshot="r", db_snapshot="d", trace=trace,
                    created_at=datetime.now(UTC))


def model_cost(values):
    return 0.002 if values.get("agent_model") == "model_cheap" else 0.01


def synthetic_engine(fails, *, heldout_fails=lambda v, c: False, max_runs=400, rate=1.0,
                     miss=None):
    """fails(values) on the incident; heldout_fails(values, case) on held-out cases. Segment
    overrides apply on the incident (tier 0) but not on held-out cases (not tier 0)."""
    def oracle(config, n, *, early_stop, case_id=None):
        values = dict(config.values)
        if case_id is None:
            for overrides in config.segment_overrides.values():
                values.update(overrides)
        bad = fails(values) if case_id is None else heldout_fails(values, case_id)
        if miss and case_id is None and miss(values, n):
            bad = False
        failures = (1 if early_stop else n) if bad and rate == 1.0 else 0
        runs = failures if early_stop and failures else n
        return Observation(runs=runs, failures=failures, passes=runs - failures,
                           cost_usd=model_cost(values) * runs)
    return SandboxSearch(lambda inc: oracle, heldout_cases=lambda inc: ["h1", "h2"],
                         max_runs=max_runs)


def prompt_fast(values):
    return values["agent_prompt_version"] == "v2_fast"


def test_engine_finds_prompt_and_segment_repair():
    result = synthetic_engine(prompt_fast).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert result.outcome is SearchOutcome.FOUND
    assert [c.flag for c in result.culprit_set] == ["agent_prompt_version"]
    kinds = [c.kind for c in result.candidates]
    assert kinds == [RepairKind.SEGMENT_OVERRIDE, RepairKind.SURGICAL, RepairKind.GLOBAL]
    best = choose_candidate(result.candidates)
    assert best.kind is RepairKind.SEGMENT_OVERRIDE and best.cost_kept == 1.0
    assert best.validation.runs == 20 and best.heldout_runs == 6
    assert result.candidates[1].validation is None  # stop at the first passing candidate
    assert result.candidates[2].cost_kept == 0.0
    phases = [s.phase for s in result.steps]
    assert phases[:2] == [TestPhase.REPRODUCE, TestPhase.REPRODUCE]
    assert TestPhase.SEARCH in phases and TestPhase.CONFIRM in phases
    # steps + held-out for the candidate (2 cases x 3) + baseline held-out (2 x 3)
    assert result.runs_used == sum(s.runs for s in result.steps) + 6 + 6
    assert result.usd_used > 0


def test_engine_finds_pair():
    def pair(v):
        return v["agent_prompt_version"] == "v2_fast" and v["agent_tool_policy"] == "expanded"
    result = synthetic_engine(pair).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert sorted(c.flag for c in result.culprit_set) == ["agent_prompt_version",
                                                          "agent_tool_policy"]
    confirms = [s for s in result.steps if s.phase is TestPhase.CONFIRM]
    assert [s.outcome for s in confirms] == [F, P, P]


def test_engine_skips_segment_option_when_heldout_regresses():
    # The segment fix leaves v2_fast on held-out cases, which then fail: not acceptable.
    result = synthetic_engine(prompt_fast, heldout_fails=lambda v, c: prompt_fast(v)).run(
        incident(), SUSPECTS, InvestigationMode.PLAIN)
    seg, surgical = result.candidates[0], result.candidates[1]
    assert seg.validation.outcome is U and seg.heldout_passes == 0
    assert seg.baseline_heldout_passes == 6
    assert choose_candidate(result.candidates).kind is RepairKind.SURGICAL
    assert surgical.cost_kept == 1.0  # the cheap model stays


def test_engine_no_segment_outside_tier0():
    staging = {"env": "staging", "tier": 2}
    result = synthetic_engine(prompt_fast).run(incident(traits=staging), SUSPECTS,
                                               InvestigationMode.PLAIN)
    assert result.candidates[0].kind is RepairKind.SURGICAL


def test_engine_not_reproducible():
    clean = synthetic_engine(lambda v: False).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert clean.outcome is SearchOutcome.NOT_REPRODUCIBLE
    always = synthetic_engine(lambda v: True).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert always.outcome is SearchOutcome.NOT_REPRODUCIBLE  # baseline fails too
    assert synthetic_engine(prompt_fast).run(incident(), [], InvestigationMode.PLAIN).outcome \
        is SearchOutcome.NOT_REPRODUCIBLE


def test_engine_budget_exhausted_is_inconclusive():
    result = synthetic_engine(prompt_fast, max_runs=25).run(incident(), SUSPECTS,
                                                            InvestigationMode.PLAIN)
    assert result.outcome is SearchOutcome.INCONCLUSIVE
    assert result.runs_used <= 25


def test_confirm_catches_a_missed_failure():
    # The tool flag alone is the culprit, but 3-run search tests miss it: ddmin returns a
    # larger set, and the fresh 10-run set-minus-one check finds the smaller one.
    def tools(v):
        return v["agent_tool_policy"] == "expanded"
    def miss(v, n):
        return n == 3 and v["agent_prompt_version"] == "v1_strict"
    two = [s for s in SUSPECTS if s.flag in ("agent_prompt_version", "agent_tool_policy")]
    result = synthetic_engine(tools, miss=miss).run(incident(), two, InvestigationMode.PLAIN)
    assert [c.flag for c in result.culprit_set] == ["agent_tool_policy"]


def test_apply_subset_and_segment():
    values = apply_subset(dict(RELEASE), SUSPECTS, SUSPECTS[1:2]).values
    assert values["agent_prompt_version"] == "v2_fast" and values["agent_model"] == "model_flagship"
    assert incident_segment(incident()) == "tier0_production"
    assert incident_segment(incident(traits={"env": "prod", "tier": 1})) is None


# -- end to end on the scripted sandbox ------------------------------------------------------------


@pytest.fixture
def ops(tmp_path):
    return adapter(by_prompt, tmp_path)


def test_sandbox_search_on_scripted_opspilot(ops):
    inc = incident()
    engine = SandboxSearch(lambda i: SandboxOracle(ops, i, parallel=4),
                           heldout_cases=lambda i: ops.case_ids("heldout")[:2])
    result = engine.run(inc, SUSPECTS, InvestigationMode.GUIDED)
    assert result.outcome is SearchOutcome.FOUND
    assert [c.flag for c in result.culprit_set] == ["agent_prompt_version"]
    best = choose_candidate(result.candidates)
    assert best.kind is RepairKind.SEGMENT_OVERRIDE and best.validation.failures == 0
    # same cheap model as the release; the scripted v1 turns use a few more tokens
    assert best.cost_kept > 0.9


def test_global_rollback_for_bench(ops):
    inc = incident()
    release = {"changes": [{"flag": f, "new": v} for f, v in RELEASE_CHANGES.items()]}
    passes, runs, kept = evaluate_global_rollback(inc, release, adapter=ops, runs_per_case=1)
    assert runs == len(ops.case_ids("heldout")) and 0 <= passes <= runs and kept == 0.0


def test_release_config_helper_is_captured():
    assert release_config().source == "captured"


# -- review asks (Naman, board #148) ---------------------------------------------------------


def test_baseline_failing_heldout_lets_no_repair_through():
    # every held-out case fails on baseline too: the comparison means nothing, so refuse
    result = synthetic_engine(prompt_fast, heldout_fails=lambda v, c: True).run(
        incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert result.outcome is SearchOutcome.FOUND
    assert choose_candidate(result.candidates) is None
    assert all(c.validation is None or c.validation.outcome is U for c in result.candidates)


def test_heldout_stops_early_once_hopeless():
    result = synthetic_engine(prompt_fast, heldout_fails=lambda v, c: prompt_fast(v)).run(
        incident(), SUSPECTS, InvestigationMode.PLAIN)
    seg = result.candidates[0]
    assert seg.heldout_runs == 3  # first held-out case missed 3 of 3: no need for the second


def test_search_unavailable_without_llm(monkeypatch):
    from orchestrator.search_port import SearchUnavailable
    from search import engine

    monkeypatch.setattr(engine, "_ADAPTERS", {})
    for var in ("LLM_API_KEY", "MODEL_CHEAP", "MODEL_FLAGSHIP"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SearchUnavailable):
        SandboxSearch().run(incident(), SUSPECTS, InvestigationMode.PLAIN)


def test_culprit_found_at_and_per_flag_ops():
    result = synthetic_engine(prompt_fast).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert result.culprit_found_at is not None
    best = choose_candidate(result.candidates)
    assert [(o.tool, o.feature, o.segment, o.value) for o in best.ops] == [
        ("create_segment_override", "agent_prompt_version", "tier0_production", "v1_strict")]


def test_live_writes_file_refs():
    from sandbox.live import snapshot_ref

    assert snapshot_ref("data/b.json") == "file:data/b.json"
    assert snapshot_ref(r"C:\x\b.json") == r"file:C:\x\b.json"
    assert snapshot_ref("gate:Development") == "gate:Development"
    assert snapshot_ref("file:a.json") == "file:a.json"


# -- review asks (Arnab, board #150) ---------------------------------------------------------


def test_violation_then_error_counts_as_failure(tmp_path):
    from sandbox.llm import Call, LLMError, ScriptedProvider, Turn
    from sandbox.runner import run_batch_sync

    class ThenDown:
        """Restarts prod (a violation), then the provider dies mid-run."""
        def __init__(self):
            self.turns = [Turn(calls=[Call("force_restart", {"cluster": "payments-prod",
                                                             "reason": "x"})])]

        def send(self, results):
            if self.turns:
                return self.turns.pop(0)
            raise LLMError("Gemini HTTP 429")

    ops = adapter(tmp_path=tmp_path)
    ops._provider = ScriptedProvider([])
    ops._provider.open_chat = lambda **kw: ThenDown()
    batch = run_batch_sync(ops, "hero", flags_release(), 3)
    assert batch.errors == 0 and batch.failures == 3
    assert all(r.trace.error for r in batch.runs)


def flags_release():
    from sandbox.flags import release_config
    return release_config()


def test_validation_needs_success_not_just_no_violations():
    def stall(config, n, *, early_stop, case_id=None):  # clean but never does the job
        return Observation(runs=n, failures=0, passes=0)
    t = Tester(stall, Budget(100, 10))
    assert t.test(TestPhase.VALIDATE, cfg(), []).outcome is U
    assert t.test(TestPhase.CONFIRM, cfg(), []).outcome is P  # confirm only looks for failures


def test_partial_failure_survives_unaffordable_topup():
    calls = {"n": 0}
    def oracle(config, n, *, early_stop, case_id=None):
        calls["n"] += 1
        return Observation(runs=1, failures=1, errors=n - 1)
    t = Tester(oracle, Budget(max_runs=10))
    assert t.test(TestPhase.CONFIRM, cfg(), []).outcome is F


def test_budget_prices_the_whole_batch():
    b = Budget(max_runs=100, max_usd=1.0, usd=0.95)
    assert b.can_afford(10) and not b.can_afford(10, cost_per_run=0.01)


def test_cache_key_includes_identity():
    from contracts.models import Identity
    from sandbox.cache import cache_key
    from sandbox.flags import release_config

    a = cache_key(release_config(), "hero", 0, model="m", prompt_hash="p",
                  identity=Identity(identifier="a"))
    b = cache_key(release_config(), "hero", 0, model="m", prompt_hash="p",
                  identity=Identity(identifier="b"))
    assert a != b


# -- determinism (Naman, board #160) ----------------------------------------------------------


def test_identical_runs_are_one_behaviour_not_a_rate():
    line = evidence_line(0, 20, distinct=1)
    assert "all identical" in line and "95%" not in line
    assert evidence_line(0, 20, distinct=4).endswith("(4 distinct behaviours)")
    assert "95% confidence" in evidence_line(0, 20, distinct=4)
    assert evidence_line(2, 20, distinct=1) == "2 failures in 20 runs"


def test_sandbox_oracle_counts_distinct_behaviours(tmp_path):
    from sandbox.flags import baseline_config
    from tests.unit.sandbox_helpers import adapter as scripted

    ops = scripted(tmp_path=tmp_path)
    oracle = SandboxOracle(ops, incident(), parallel=2)
    obs = oracle(baseline_config(), 4, early_stop=False)
    assert obs.runs == 4 and len(obs.behaviours) == 1  # a scripted agent is deterministic


def test_distinct_behaviours_reach_the_test_result(tmp_path):
    from sandbox.flags import baseline_config
    from tests.unit.sandbox_helpers import adapter as scripted

    ops = scripted(tmp_path=tmp_path)
    t = Tester(SandboxOracle(ops, incident(), parallel=2), Budget(100, 10))
    confirm = t.test(TestPhase.CONFIRM, baseline_config(), [])
    assert confirm.distinct_behaviours == 1  # scripted agent: every run identical
    provisional = t.test(TestPhase.SEARCH, baseline_config(), [], record=False)
    assert t.escalate(provisional).distinct_behaviours == 1
    synthetic = Tester(oracle_from(lambda v, c: False), Budget(100, 10))
    assert synthetic.test(TestPhase.CONFIRM, cfg(), []).distinct_behaviours is None


# -- adaptive stop (Naman, board #170) -----------------------------------------------------------


def oracle_with_behaviours(fails, behaviour_of_run):
    """behaviour_of_run(i) -> signature for the i-th run overall."""
    state = {"i": 0}

    def oracle(config, n, *, early_stop, case_id=None):
        runs = failures = 0
        seen = set()
        for _ in range(n):
            seen.add(behaviour_of_run(state["i"]))
            state["i"] += 1
            runs += 1
            if fails(config.values):
                failures += 1
                if early_stop:
                    break
        return Observation(runs=runs, failures=failures, passes=runs - failures, behaviours=seen)
    return oracle


def test_adaptive_stops_after_three_identical_runs():
    t = Tester(oracle_with_behaviours(lambda v: False, lambda i: "same"), Budget(100, 10),
               adaptive=True)
    r = t.test(TestPhase.VALIDATE, cfg(), [])
    assert r.outcome is P and r.runs == 3 and r.distinct_behaviours == 1
    assert "all identical" in evidence_line(r.failures, r.runs, r.distinct_behaviours)
    rep = t.test(TestPhase.REPRODUCE, cfg(x=1), [])
    assert rep.runs == 3 and t.budget.runs == 6


def test_adaptive_tops_up_when_runs_differ():
    t = Tester(oracle_with_behaviours(lambda v: False, lambda i: f"b{i % 2}"), Budget(100, 10),
               adaptive=True)
    r = t.test(TestPhase.CONFIRM, cfg(), [])
    assert r.outcome is P and r.runs == 10 and r.distinct_behaviours == 2


def test_adaptive_failures_and_search_unchanged():
    t = Tester(oracle_with_behaviours(lambda v: True, lambda i: "same"), Budget(100, 10),
               adaptive=True)
    assert t.test(TestPhase.REPRODUCE, cfg(), []).outcome is F
    assert t.test(TestPhase.SEARCH, cfg(), []).runs == 1  # search keeps early stop, 3 max
    off = Tester(oracle_with_behaviours(lambda v: False, lambda i: "same"), Budget(100, 10))
    assert off.test(TestPhase.VALIDATE, cfg(), []).runs == 20  # off by default


def test_adaptive_engine_uses_far_fewer_runs(tmp_path, monkeypatch):
    from tests.unit.sandbox_helpers import adapter as scripted

    ops = scripted(tmp_path=tmp_path)
    def engine(adaptive):
        return SandboxSearch(lambda i: SandboxOracle(ops, i, parallel=4),
                             heldout_cases=lambda i: ops.case_ids("heldout")[:2],
                             adaptive=adaptive)
    full = engine(False).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    fast = engine(True).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert [c.flag for c in fast.culprit_set] == [c.flag for c in full.culprit_set] \
        == ["agent_prompt_version"]
    assert choose_candidate(fast.candidates).kind is choose_candidate(full.candidates).kind
    assert fast.runs_used < full.runs_used * 0.6
    monkeypatch.setenv("SEARCH_ADAPTIVE", "1")
    assert SandboxSearch().adaptive is True


# -- baseline held-out cache (opt-in) ---------------------------------------------------------------


def test_heldout_cache_reuses_the_baseline_only():
    from search import repair

    repair.clear_baseline_cache()
    try:
        first_engine, second_engine = synthetic_engine(prompt_fast), synthetic_engine(prompt_fast)
        first_engine.heldout_cache = second_engine.heldout_cache = True
        first = first_engine.run(incident(), SUSPECTS, InvestigationMode.PLAIN)
        second = second_engine.run(incident(), SUSPECTS, InvestigationMode.PLAIN)
        # the second investigation skips the baseline's 2 cases x 3 runs and nothing else
        assert second.runs_used == first.runs_used - 6
        a, b = choose_candidate(first.candidates), choose_candidate(second.candidates)
        assert (b.kind, b.heldout_passes, b.heldout_runs, b.baseline_heldout_passes) == \
            (a.kind, a.heldout_passes, a.heldout_runs, a.baseline_heldout_passes)
        assert b.heldout_runs == 6  # the repair's own held-out runs are fresh
    finally:
        repair.clear_baseline_cache()


def test_heldout_cache_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SEARCH_HELDOUT_CACHE", raising=False)
    first = synthetic_engine(prompt_fast).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    second = synthetic_engine(prompt_fast).run(incident(), SUSPECTS, InvestigationMode.PLAIN)
    assert second.runs_used == first.runs_used
    monkeypatch.setenv("SEARCH_HELDOUT_CACHE", "1")
    assert synthetic_engine(prompt_fast).heldout_cache is True


def test_heldout_cache_never_keeps_a_failing_or_other_baseline():
    from search import repair
    from search.engine import _baseline_key

    repair.clear_baseline_cache()
    try:
        failing = synthetic_engine(prompt_fast, heldout_fails=lambda v, c: True)
        failing.heldout_cache = True
        failing.run(incident(), SUSPECTS, InvestigationMode.PLAIN)
        assert repair._BASELINE_CACHE == {}  # a baseline that fails held-out is measured again
        base = FlagConfig(values=dict(BASELINE), source="override")
        other = FlagConfig(values={**BASELINE, "agent_max_steps": 99}, source="override")
        assert _baseline_key(incident(), ["h1"], base) != _baseline_key(incident(), ["h1"], other)
        assert _baseline_key(incident(), ["h1"], base) != _baseline_key(incident(), ["h2"], base)
    finally:
        repair.clear_baseline_cache()
