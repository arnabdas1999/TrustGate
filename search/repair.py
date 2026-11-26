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
    WriteOp,
)
from orchestrator.snapshots import same_value
from search.testing import RUNS, Tester

HELDOUT_RUNS = 3
HELDOUT_SLACK = 1  # "within 1 run of baseline"
# The baseline must pass held-out itself, or comparing a repair against it means nothing.
MIN_BASELINE_RATE = 0.8


def revert(values: Mapping[str, FlagValue], changes: Sequence[FlagChange]) -> dict[str, FlagValue]:
    out = dict(values)
    for c in changes:
        if c.old is None:
            out.pop(c.flag, None)
        else:
            out[c.flag] = c.old
    return out


def candidate_config(kind: RepairKind, captured: Mapping[str, FlagValue],
                     suspects: Sequence[FlagChange], group: Sequence[FlagChange],
                     segment: str | None) -> FlagConfig | None:
    """The replay config for reverting `group` (culprit flags) the `kind` way."""
    if kind is RepairKind.SEGMENT_OVERRIDE:
        if not segment or any(c.old is None for c in group):
            return None
        return FlagConfig(values=dict(captured), source=ConfigSource.OVERRIDE,
                          segment_overrides={segment: {c.flag: c.old for c in group}})
    changes = group if kind is RepairKind.SURGICAL else suspects
    return FlagConfig(values=revert(captured, changes), source=ConfigSource.OVERRIDE)


def plans(culprit: Sequence[FlagChange], suspects: Sequence[FlagChange],
          segment: str | None) -> list[list[tuple[RepairKind, list[FlagChange]]]]:
    """Repair options in tiers, narrowest first. A pair culprit gets per-flag options before
    the whole set (reverting either flag of a 1-minimal pair breaks the failure); options in
    one tier are all validated so the held-out cases can decide between them."""
    groups = [[c] for c in culprit] if len(culprit) > 1 else []
    tiers: list[list[tuple[RepairKind, list[FlagChange]]]] = []
    for kind in (RepairKind.SEGMENT_OVERRIDE, RepairKind.SURGICAL):
        if kind is RepairKind.SEGMENT_OVERRIDE and not segment:
            continue
        if groups:
            tiers.append([(kind, g) for g in groups])
        tiers.append([(kind, list(culprit))])
    tiers.append([(RepairKind.GLOBAL, list(suspects))])
    return tiers


def ops_for(kind: RepairKind, group: Sequence[FlagChange], suspects: Sequence[FlagChange],
            segment: str | None) -> list[WriteOp]:
    """Which flags this option reverts, as WriteOps. Project and environment are left blank:
    the orchestrator rebuilds every op from its own config, triage and the baseline, and checks
    it against validation.config."""
    base = {"project": "", "environment": ""}
    if kind is RepairKind.SEGMENT_OVERRIDE:
        return [WriteOp(tool="create_segment_override", feature=c.flag, segment=segment,
                        value=c.old, **base) for c in sorted(group, key=lambda c: c.flag)]
    changes = group if kind is RepairKind.SURGICAL else suspects
    return [WriteOp(tool="update_feature_state", feature=c.flag, value=c.old, **base)
            for c in sorted(changes, key=lambda c: c.flag)]


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
    """Held-out cases, run under a configuration 3 times each. Baseline is measured once and
    must itself pass held-out (MIN_BASELINE_RATE), or no repair can be judged against it."""

    def __init__(self, tester: Tester, cases: Sequence[str], baseline: FlagConfig):
        self.tester, self.cases, self.baseline = tester, list(cases), baseline
        self._baseline: tuple[int, int] | None = None

    def passes(self, config: FlagConfig, *, max_misses: int | None = None
               ) -> tuple[int, int] | None:
        """(passes, runs) over every held-out case. With max_misses, stops as soon as more runs
        missed than that, since the repair can no longer come within the slack."""
        total = runs = 0
        for case_id in self.cases:
            obs = self.tester.observe(config, HELDOUT_RUNS, early_stop=False, case_id=case_id)
            if obs is None or obs.passes is None or obs.runs < HELDOUT_RUNS:
                return None
            total += obs.passes
            runs += obs.runs
            if max_misses is not None and runs - total > max_misses:
                break
        return total, runs

    def max_misses(self) -> int | None:
        base = self.baseline_passes()
        if base is None:
            return None
        return (len(self.cases) * HELDOUT_RUNS - base[0]) + HELDOUT_SLACK

    def baseline_passes(self) -> tuple[int, int] | None:
        if self._baseline is None:
            self._baseline = self.passes(self.baseline)
        return self._baseline

    def judge(self, held: tuple[int, int] | None) -> bool:
        base = self.baseline_passes() if held is not None else None
        if held is None or base is None or not base[1]:
            return False
        if base[0] < MIN_BASELINE_RATE * base[1]:
            return False  # the baseline fails held-out itself: nothing to compare against
        if held[1] < base[1]:
            return False  # stopped early: too many misses already
        return held[0] >= base[0] - HELDOUT_SLACK


def validate(tester: Tester, *, captured: Mapping[str, FlagValue],
             suspects: Sequence[FlagChange], culprit: Sequence[FlagChange],
             segment: str | None, heldout: HeldOut, costs: CostRef,
             stop_at_first: bool = True) -> list[RepairCandidate]:
    out: list[RepairCandidate] = []
    found = False
    for tier in plans(culprit, suspects, segment):
        validated: list[RepairCandidate] = []
        for kind, group in tier:
            cfg = candidate_config(kind, captured, suspects, group, segment)
            if cfg is None:
                continue
            ops = ops_for(kind, group, suspects, segment)
            if found and stop_at_first:
                out.append(RepairCandidate(kind=kind, ops=ops,
                                           cost_kept=0.0 if kind is RepairKind.GLOBAL else None))
                continue
            changed = [s for s in suspects if same_value(cfg.values.get(s.flag), s.new)]
            incident = tester.test(TestPhase.VALIDATE, cfg, changed,
                                   n=RUNS[TestPhase.VALIDATE], record=False)
            obs = tester.last
            option_cost = obs.cost_usd / obs.runs if obs and obs.runs else None
            held = None
            if incident.outcome is TestOutcome.PASS:
                held = heldout.passes(cfg, max_misses=heldout.max_misses())
            heldout_ok = heldout.judge(held)
            if incident.outcome is TestOutcome.PASS and not heldout_ok:
                # Clean on the incident but held-out regressed, the baseline failed held-out
                # too, or it couldn't be measured: not a passing repair.
                incident = incident.model_copy(update={"outcome": TestOutcome.INCONCLUSIVE})
            tester.steps.append(incident)
            base = heldout.baseline_passes() if held is not None else None
            kept = 0.0 if kind is RepairKind.GLOBAL else cost_kept(
                costs.baseline_per_run, costs.release_per_run, option_cost)
            validated.append(RepairCandidate(
                kind=kind, ops=ops, validation=incident, violations=incident.failures,
                heldout_passes=held[0] if held else None, heldout_runs=held[1] if held else None,
                baseline_heldout_passes=base[0] if base else None, cost_kept=kept))
        # Within a tier, passing options first: more held-out passes, then more cost kept.
        validated.sort(key=lambda c: (c.validation.outcome is not TestOutcome.PASS,
                                      -(c.heldout_passes or 0), -(c.cost_kept or 0.0)))
        out.extend(validated)
        found = found or any(c.validation.outcome is TestOutcome.PASS for c in validated)
    return out
