"""Tiered testing: one configuration -> PASS, FAIL or INCONCLUSIVE, never a bare boolean.

The grader is deterministic, so one observed violation is real evidence (FAIL). A clean result
is only statistical, so the number of runs grows with how much an answer matters:

    reproduce   10 on release, 10 on baseline
    search      3 per candidate subset; any violation is FAIL, 0 of 3 is provisional
    escalate    7 more (10 in total) for a provisional result that decides the answer
    confirm     fresh 10 on the culprit set and on each set-minus-one
    validate    20 incident replays plus each held-out case 3 times

Errored runs (provider down) never count: the tester tops them up, and a configuration that
can't get enough completed runs within the budget is INCONCLUSIVE.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from contracts.models import FlagChange, FlagConfig, TestOutcome, TestPhase, TestResult
from search.budget import Budget

# A clean run in these phases must also be a graded success (the agent did the right thing),
# not just free of violations: a stalled agent breaks no rule and fixes nothing.
NEEDS_SUCCESS = {TestPhase.VALIDATE, TestPhase.POST_WRITE}

RUNS = {TestPhase.REPRODUCE: 10, TestPhase.SEARCH: 3, TestPhase.ESCALATE: 7,
        TestPhase.CONFIRM: 10, TestPhase.VALIDATE: 20, TestPhase.POST_WRITE: 5}
EARLY_STOP = {TestPhase.SEARCH, TestPhase.ESCALATE, TestPhase.VALIDATE}
MAX_TOPUPS = 2


@dataclass
class Observation:
    """What one batch of replays produced. `runs` counts completed runs only."""

    runs: int
    failures: int
    errors: int = 0
    passes: int | None = None  # graded PASS (no violations AND success), when known
    cost_usd: float = 0.0
    trace_ids: list[str] = field(default_factory=list)
    # One signature per distinct behaviour seen (the tool-call sequence with arguments).
    # At temperature 0 every run of a config is often identical: then N runs are one
    # observation repeated, and the evidence line must say so.
    behaviours: set[str] = field(default_factory=set)


class Oracle(Protocol):
    def __call__(self, config: FlagConfig, n: int, *, early_stop: bool,
                 case_id: str | None = None) -> Observation: ...


def rule_of_three(n: int) -> float:
    """95% upper bound on the failure rate after 0 failures in n runs."""
    return 3.0 / n if n else 1.0


def evidence_line(failures: int, runs: int, distinct: int | None = None) -> str:
    """The proposal card's evidence. The rule of three assumes independent runs, so it is only
    quoted when the runs actually differed; identical runs are reported as one behaviour."""
    if failures:
        return f"{failures} failures in {runs} runs"
    if distinct == 1 and runs > 1:
        return (f"0 failures in {runs} runs, all identical (the agent runs at temperature 0), "
                "so this is one behaviour observed, not a failure-rate estimate")
    line = (f"0 failures in {runs} runs. With 95% confidence the failure rate is below "
            f"about {rule_of_three(runs):.0%}")
    if distinct is not None:
        line += f" ({distinct} distinct behaviours)"
    return line


@dataclass
class Tester:
    __test__ = False  # not a pytest test class

    oracle: Oracle
    budget: Budget
    steps: list[TestResult] = field(default_factory=list)
    usd: float = 0.0
    last: Observation | None = None  # the observation behind the latest test()
    _spent_runs: int = 0
    _spent_usd: float = 0.0

    @property
    def cost_per_run(self) -> float:
        """Measured so far; the budget prices a whole batch with it before starting."""
        return self._spent_usd / self._spent_runs if self._spent_runs else 0.0

    def observe(self, config: FlagConfig, n: int, *, early_stop: bool,
                case_id: str | None = None) -> Observation | None:
        """n completed runs (fewer only on early stop), topping up errored runs. None means
        the budget could not cover it."""
        total = Observation(runs=0, failures=0, passes=0)
        need = n
        for _ in range(1 + MAX_TOPUPS):
            if not self.budget.can_afford(need, self.cost_per_run):
                # keep a failure already observed: one violation is real evidence
                return total if total.failures else None
            obs = self.oracle(config, need, early_stop=early_stop, case_id=case_id)
            self.budget.charge(obs.runs + obs.errors, obs.cost_usd)
            self._spent_runs += obs.runs + obs.errors
            self._spent_usd += obs.cost_usd
            self.usd = round(self.usd + obs.cost_usd, 6)
            total.runs += obs.runs
            total.failures += obs.failures
            total.errors += obs.errors
            total.cost_usd += obs.cost_usd
            total.trace_ids += obs.trace_ids
            total.behaviours |= obs.behaviours
            if total.passes is not None:
                total.passes = None if obs.passes is None else total.passes + obs.passes
            if (early_stop and total.failures) or total.runs >= n:
                return total
            need = n - total.runs
        return total if total.runs >= n or total.failures else None

    def test(self, phase: TestPhase, config: FlagConfig, changes: Sequence[FlagChange], *,
             n: int | None = None, record: bool = True) -> TestResult:
        n = n or RUNS[phase]
        obs = self.observe(config, n, early_stop=phase in EARLY_STOP)
        self.last = obs
        if obs is None:
            result = TestResult(phase=phase, config=config, changes=list(changes), runs=0,
                                failures=0, outcome=TestOutcome.INCONCLUSIVE)
        else:
            stalled = phase in NEEDS_SUCCESS and obs.passes is not None \
                and obs.passes < obs.runs - obs.failures
            if obs.failures:
                outcome = TestOutcome.FAIL
            elif obs.runs >= n and not stalled:
                outcome = TestOutcome.PASS
            else:  # too few runs, or clean but the agent didn't do the right thing
                outcome = TestOutcome.INCONCLUSIVE
            result = TestResult(phase=phase, config=config, changes=list(changes), runs=obs.runs,
                                failures=obs.failures, outcome=outcome, trace_ids=obs.trace_ids)
        if record:
            self.steps.append(result)
        return result

    def search_test(self, config: FlagConfig, changes: Sequence[FlagChange]) -> TestOutcome:
        """3 runs. A violation is FAIL. A clean 0 of 3 is only provisional: it's recorded as
        INCONCLUSIVE in the steps (that's what it is) and returned to ddmin as PASS, because
        the final answer is re-checked on fresh runs in the confirm step."""
        result = self.test(TestPhase.SEARCH, config, changes, record=False)
        if result.outcome is TestOutcome.PASS:
            self.steps.append(result.model_copy(update={"outcome": TestOutcome.INCONCLUSIVE}))
            return TestOutcome.PASS
        self.steps.append(result)
        return result.outcome

    def escalate(self, provisional: TestResult) -> TestResult:
        """7 more runs on a provisional PASS. Still 0 in 10 is PASS; budget gone, INCONCLUSIVE."""
        more = self.test(TestPhase.ESCALATE, provisional.config, provisional.changes,
                         record=False)
        runs, failures = provisional.runs + more.runs, provisional.failures + more.failures
        if more.outcome is TestOutcome.INCONCLUSIVE and not failures:
            outcome = TestOutcome.INCONCLUSIVE
        else:
            outcome = TestOutcome.FAIL if failures else TestOutcome.PASS
        result = TestResult(phase=TestPhase.ESCALATE, config=provisional.config,
                            changes=provisional.changes, runs=runs, failures=failures,
                            outcome=outcome, trace_ids=provisional.trace_ids + more.trace_ids)
        self.steps.append(result)
        return result
