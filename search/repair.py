"""Repair candidates, narrowest first, each validated on fresh runs.

    segment_override  culprit flags back to baseline for the incident's segment only
    surgical          culprit flags back to baseline for everyone
    global            every release change back to baseline

A candidate passes when the incident shows 0 violations in 20 fresh replays AND the held-out
cases (3 runs each) pass within 1 run of the baseline on the same cases. Validation stops at
the first passing candidate; the wider ones are listed unvalidated so the card can show them.

cost kept = (baseline cost - option cost) / (baseline cost - release cost), from measured
tokens per run. 1.0 keeps the whole saving, 0.0 keeps none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from contracts.models import (
    ConfigSource,
    FlagChange,
    FlagConfig,
    FlagValue,
    RepairCandidate,
    RepairKind,
    TestOutcome,
    TestPhase,
)
from search.testing import RUNS, Tester

HELDOUT_RUNS = 3
HELDOUT_SLACK = 1  # "within 1 run of baseline"


def revert(values: Mapping[str, FlagValue], changes: Sequence[FlagChange]) -> dict[str, FlagValue]:
    out = dict(values)
    for c in changes:
        if c.old is None:
            out.pop(c.flag, None)
        else:
            out[c.flag] = c.old
    return out


def candidate_config(kind: RepairKind, captured: Mapping[str, FlagValue],
                     suspects: Sequence[FlagChange], culprit: Sequence[FlagChange],
                     segment: str | None) -> FlagConfig | None:
    if kind is RepairKind.SEGMENT_OVERRIDE:
        if not segment or any(c.old is None for c in culprit):
            return None
        return FlagConfig(values=dict(captured), source=ConfigSource.OVERRIDE,
                          segment_overrides={segment: {c.flag: c.old for c in culprit}})
    changes = culprit if kind is RepairKind.SURGICAL else suspects
    return FlagConfig(values=revert(captured, changes), source=ConfigSource.OVERRIDE)


def cost_kept(baseline: float | None, release: float | None, option: float | None
              ) -> float | None:
    if baseline is None or release is None or option is None or baseline - release <= 0:
        return None
    return round(min(1.0, max(0.0, (baseline - option) / (baseline - release))), 3)


@dataclass
class CostRef:
    baseline_per_run: float | None
    release_per_run: float | None


class HeldOut:
    """Held-out cases, run under a configuration 3 times each. Baseline is measured once."""

    def __init__(self, tester: Tester, cases: Sequence[str], baseline: FlagConfig):
        self.tester, self.cases, self.baseline = tester, list(cases), baseline
        self._baseline_passes: int | None = None

    def passes(self, config: FlagConfig) -> tuple[int, int] | None:
        total = runs = 0
        for case_id in self.cases:
            obs = self.tester.observe(config, HELDOUT_RUNS, early_stop=False, case_id=case_id)
            if obs is None or obs.passes is None or obs.runs < HELDOUT_RUNS:
                return None
            total += obs.passes
            runs += obs.runs
        return total, runs

    def baseline_passes(self) -> int | None:
        if self._baseline_passes is None:
            got = self.passes(self.baseline)
            self._baseline_passes = None if got is None else got[0]
        return self._baseline_passes


def validate(tester: Tester, *, captured: Mapping[str, FlagValue],
             suspects: Sequence[FlagChange], culprit: Sequence[FlagChange],
             segment: str | None, heldout: HeldOut, costs: CostRef,
             stop_at_first: bool = True) -> list[RepairCandidate]:
    out: list[RepairCandidate] = []
    found = False
    for kind in (RepairKind.SEGMENT_OVERRIDE, RepairKind.SURGICAL, RepairKind.GLOBAL):
        cfg = candidate_config(kind, captured, suspects, culprit, segment)
        if cfg is None:
            continue
        changed = [s for s in suspects if cfg.values.get(s.flag) == s.new]
        kept_if_global = 0.0 if kind is RepairKind.GLOBAL else None
        if found and stop_at_first:
            out.append(RepairCandidate(kind=kind, ops=[], cost_kept=kept_if_global))
            continue
        incident = tester.test(TestPhase.VALIDATE, cfg, changed, n=RUNS[TestPhase.VALIDATE],
                               record=False)
        obs = tester.last
        option_cost = obs.cost_usd / obs.runs if obs and obs.runs else None
        held = heldout.passes(cfg) if incident.outcome is TestOutcome.PASS else None
        base = heldout.baseline_passes() if held is not None else None
        heldout_ok = held is not None and base is not None and held[0] >= base - HELDOUT_SLACK
        if incident.outcome is TestOutcome.PASS and not heldout_ok:
            # Clean on the incident but held-out regressed (or couldn't be measured).
            incident = incident.model_copy(update={"outcome": TestOutcome.INCONCLUSIVE})
        tester.steps.append(incident)
        kept = kept_if_global if kind is RepairKind.GLOBAL else cost_kept(
            costs.baseline_per_run, costs.release_per_run, option_cost)
        out.append(RepairCandidate(
            kind=kind, ops=[], validation=incident, violations=incident.failures,
            heldout_passes=held[0] if held else None, heldout_runs=held[1] if held else None,
            baseline_heldout_passes=base, cost_kept=kept))
        found = found or (incident.outcome is TestOutcome.PASS and heldout_ok)
    return out
