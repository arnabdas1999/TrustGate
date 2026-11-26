"""The search engine the orchestrator calls: search.run(incident, suspects, mode).

    1. reproduce   10 runs on the captured release config, 10 on baseline. The release must
                   fail at least once and the baseline never, or the answer is NOT_REPRODUCIBLE.
    2. ddmin       over the suspects in the order given (guided: LLM order; plain: diff
                   order), 3 runs per subset.
    3. confirm     fresh 10 on the culprit set (must fail) and on each set-minus-one (must be
                   0 of 10). A failing set-minus-one restarts ddmin on that smaller set.
    4. repair      narrowest first (search/repair.py).

Replays use the incident's captured configuration plus overrides, on the sandbox. Live
Flagsmith is never touched, and the model under test is never swapped.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import count

from agent.triage import triage
from contracts.models import (
    ConfigSource,
    FlagChange,
    FlagConfig,
    Incident,
    InvestigationMode,
    TestOutcome,
    TestPhase,
)
from orchestrator.search_port import SearchOutcome, SearchResult
from sandbox.adapter import TargetAppAdapter
from sandbox.flags import SEGMENTS
from sandbox.runner import run_batch_sync
from search.budget import Budget
from search.ddmin import ddmin
from search.repair import CostRef, HeldOut, revert, validate
from search.testing import Observation, Oracle, Tester

MAX_CONFIRM_ROUNDS = 4


def apply_subset(captured: dict, suspects: Sequence[FlagChange],
                 subset: Sequence[FlagChange]) -> FlagConfig:
    """The captured config with every suspect NOT in `subset` reverted to its old value."""
    keep = {c.flag for c in subset}
    return FlagConfig(values=revert(captured, [s for s in suspects if s.flag not in keep]),
                      source=ConfigSource.OVERRIDE)


def incident_segment(incident: Incident) -> str | None:
    """Deterministic triage on the incident's trusted traits, with the sandbox's segments."""
    doc = {"segments": {name: {"rules": seg["rules"]} for name, seg in SEGMENTS.items()}}
    return triage(doc, incident.identity.traits)


class SandboxOracle:
    """Runs replays on the sandbox. Each call gets fresh seeds, so no two runs repeat."""

    def __init__(self, adapter: TargetAppAdapter, incident: Incident, *,
                 parallel: int | None = None):
        self.adapter, self.incident, self.parallel = adapter, incident, parallel
        self._seeds = count(0, 1000)

    def __call__(self, config: FlagConfig, n: int, *, early_stop: bool,
                 case_id: str | None = None) -> Observation:
        case = case_id or self.incident.case_id
        identity = self.incident.identity if case_id is None else self.adapter.identity(case)
        batch = run_batch_sync(self.adapter, case, config, n, identity=identity,
                               early_stop=early_stop, parallel=self.parallel,
                               seed_base=next(self._seeds))
        return Observation(runs=len(batch.completed), failures=batch.failures,
                           errors=batch.errors, passes=batch.passes, cost_usd=batch.cost_usd,
                           trace_ids=[r.trace.run_id for r in batch.runs])


class SandboxSearch:
    """Implements orchestrator.search_port.SearchEngine."""

    def __init__(self, oracle_factory: Callable[[Incident], Oracle] | None = None, *,
                 heldout_cases: Callable[[Incident], list[str]] | None = None,
                 max_runs: int = 160, max_usd: float = 5.0, stop_at_first: bool = True):
        self.oracle_factory = oracle_factory or _sandbox_oracle
        self.heldout_cases = heldout_cases or _sandbox_heldout
        self.max_runs, self.max_usd, self.stop_at_first = max_runs, max_usd, stop_at_first

    def run(self, incident: Incident, suspects: Sequence[FlagChange],
            mode: InvestigationMode) -> SearchResult:
        suspects = list(suspects)  # guided order comes from the caller; plain is diff order
        tester = Tester(self.oracle_factory(incident), Budget(self.max_runs, self.max_usd))
        captured = dict(incident.resolved_flags.values)

        def done(outcome: SearchOutcome, culprit=(), candidates=()) -> SearchResult:
            return SearchResult(outcome=outcome, culprit_set=list(culprit), steps=tester.steps,
                                candidates=list(candidates), runs_used=tester.budget.runs,
                                usd_used=tester.usd)

        if not suspects:
            return done(SearchOutcome.NOT_REPRODUCIBLE)

        # 1. reproduce
        release = tester.test(TestPhase.REPRODUCE, apply_subset(captured, suspects, suspects),
                              suspects)
        release_cost = _per_run(tester.last)
        baseline_cfg = apply_subset(captured, suspects, [])
        baseline = tester.test(TestPhase.REPRODUCE, baseline_cfg, [])
        baseline_cost = _per_run(tester.last)
        if TestOutcome.INCONCLUSIVE in (release.outcome, baseline.outcome):
            return done(SearchOutcome.INCONCLUSIVE)
        if release.outcome is not TestOutcome.FAIL or baseline.outcome is TestOutcome.FAIL:
            return done(SearchOutcome.NOT_REPRODUCIBLE)

        # 2 + 3. ddmin, then confirm 1-minimality on fresh runs
        def search_test(subset: list[FlagChange]) -> TestOutcome:
            return tester.search_test(apply_subset(captured, suspects, subset), subset).outcome

        current = suspects
        culprit: list[FlagChange] | None = None
        for _ in range(MAX_CONFIRM_ROUNDS):
            dd = ddmin(current, search_test) if len(current) > 1 else None
            if dd is not None and dd.outcome is TestOutcome.INCONCLUSIVE:
                return done(SearchOutcome.INCONCLUSIVE)
            found = dd.culprit if dd else current
            confirm = tester.test(TestPhase.CONFIRM, apply_subset(captured, suspects, found),
                                  found)
            if confirm.outcome is not TestOutcome.FAIL:
                return done(SearchOutcome.INCONCLUSIVE)
            smaller = None
            for drop in found if len(found) > 1 else []:
                rest = [c for c in found if c is not drop]
                r = tester.test(TestPhase.CONFIRM, apply_subset(captured, suspects, rest), rest)
                if r.outcome is TestOutcome.INCONCLUSIVE:
                    return done(SearchOutcome.INCONCLUSIVE)
                if r.outcome is TestOutcome.FAIL:
                    smaller = rest
                    break
            if smaller is None:
                culprit = found
                break
            current = smaller
        if culprit is None:
            return done(SearchOutcome.INCONCLUSIVE)

        # 4. repair
        try:
            segment = incident_segment(incident)
        except Exception:  # noqa: BLE001 (no segment: the segment option is skipped)
            segment = None
        heldout = HeldOut(tester, self.heldout_cases(incident), baseline_cfg)
        candidates = validate(tester, captured=captured, suspects=suspects, culprit=culprit,
                              segment=segment, heldout=heldout,
                              costs=CostRef(baseline_cost, release_cost),
                              stop_at_first=self.stop_at_first)
        return done(SearchOutcome.FOUND, culprit, candidates)


def _per_run(obs: Observation | None) -> float | None:
    return obs.cost_usd / obs.runs if obs and obs.runs else None


_ADAPTERS: dict[str, TargetAppAdapter] = {}


def _adapter(domain: str) -> TargetAppAdapter:
    from sandbox import get_adapter

    if domain not in _ADAPTERS:
        _ADAPTERS[domain] = get_adapter(domain)
    return _ADAPTERS[domain]


def _sandbox_oracle(incident: Incident) -> Oracle:
    return SandboxOracle(_adapter(incident.domain), incident)


def _sandbox_heldout(incident: Incident) -> list[str]:
    return _adapter(incident.domain).case_ids("heldout")


def run(incident: Incident, suspects: Sequence[FlagChange],
        mode: InvestigationMode = InvestigationMode.GUIDED) -> SearchResult:
    """The plan's interface: search.run(incident, suspects, mode)."""
    return SandboxSearch().run(incident, suspects, mode)
