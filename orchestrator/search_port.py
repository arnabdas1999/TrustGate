"""The orchestrator's view of the search engine (Khyati's search/), behind a small protocol.

The plan's interface is `search.run(incident, suspects, mode)`. It reproduces the failure,
runs ddmin over the suspects, and validates repair candidates narrowest first. Until search/
lands on a branch, the orchestrator runs against:
- ScriptedSearch: deterministic, names configured culprits. For tests and the plumbing demo
  only, never for the benchmark (it doesn't replay anything).
- UnavailableSearch: the default. It refuses, so nothing pretends to have searched.

Candidates carry no ops: code builds the WriteOps from the candidate's kind, the culprit set
and the segment (orchestrator/proposals.py).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from contracts.models import (
    ConfigSource,
    FlagChange,
    FlagConfig,
    Incident,
    InvestigationMode,
    RepairCandidate,
    RepairKind,
    TestOutcome,
    TestPhase,
    TestResult,
)


class SearchOutcome(StrEnum):
    FOUND = "found"
    NOT_REPRODUCIBLE = "not_reproducible"
    INCONCLUSIVE = "inconclusive"


class SearchResult(BaseModel):
    outcome: SearchOutcome
    culprit_set: list[FlagChange] = Field(default_factory=list)
    steps: list[TestResult] = Field(default_factory=list)
    candidates: list[RepairCandidate] = Field(default_factory=list)
    runs_used: int = 0
    usd_used: float = 0.0


class SearchUnavailable(Exception):
    pass


class SearchEngine(Protocol):
    def run(self, incident: Incident, suspects: Sequence[FlagChange],
            mode: InvestigationMode) -> SearchResult: ...


class UnavailableSearch:
    def run(self, incident: Incident, suspects: Sequence[FlagChange],
            mode: InvestigationMode) -> SearchResult:
        raise SearchUnavailable("no search engine configured (search/ is not on this branch)")


def _config(changes: Sequence[FlagChange]) -> FlagConfig:
    return FlagConfig(values={c.flag: c.new for c in changes if c.new is not None},
                      source=ConfigSource.OVERRIDE)


class ScriptedSearch:
    """Deterministic stand-in: the configured flags are the culprits, every check is scripted."""

    def __init__(self, culprits: Sequence[str]):
        self.culprits = list(culprits)

    def run(self, incident: Incident, suspects: Sequence[FlagChange],
            mode: InvestigationMode) -> SearchResult:
        release = list(suspects)
        culprit = [s for s in release if s.flag in self.culprits]
        reproduce = TestResult(phase=TestPhase.REPRODUCE, config=_config(release), changes=release,
                               runs=10, failures=9, outcome=TestOutcome.FAIL)
        if not culprit or len(culprit) != len(self.culprits):
            pass_ = reproduce.model_copy(update={"failures": 0, "outcome": TestOutcome.PASS})
            return SearchResult(outcome=SearchOutcome.NOT_REPRODUCIBLE, steps=[pass_],
                                runs_used=pass_.runs)
        steps = [reproduce]
        for s in release:  # one diagnostic step per suspect, culprit fails
            fails = s in culprit
            steps.append(TestResult(phase=TestPhase.SEARCH, config=_config([s]), changes=[s],
                                    runs=3, failures=2 if fails else 0,
                                    outcome=TestOutcome.FAIL if fails else TestOutcome.PASS))
        steps.append(TestResult(phase=TestPhase.CONFIRM, config=_config(culprit), changes=culprit,
                                runs=10, failures=8, outcome=TestOutcome.FAIL))
        validation = TestResult(phase=TestPhase.VALIDATE, config=_config(release), changes=release,
                                runs=20, failures=0, outcome=TestOutcome.PASS)
        candidates = [
            RepairCandidate(kind=RepairKind.SEGMENT_OVERRIDE, ops=[], validation=validation,
                            heldout_passes=21, heldout_runs=21, baseline_heldout_passes=21,
                            cost_kept=1.0, violations=0),
            RepairCandidate(kind=RepairKind.SURGICAL, ops=[], validation=validation,
                            heldout_passes=20, heldout_runs=21, baseline_heldout_passes=21,
                            cost_kept=0.8, violations=0),
            RepairCandidate(kind=RepairKind.GLOBAL, ops=[], validation=validation,
                            heldout_passes=18, heldout_runs=21, baseline_heldout_passes=21,
                            cost_kept=0.0, violations=0),
        ]
        steps.append(validation)
        return SearchResult(outcome=SearchOutcome.FOUND, culprit_set=culprit, steps=steps,
                            candidates=candidates, runs_used=sum(s.runs for s in steps))


def search_from_env(env: Mapping[str, str] | None = None) -> SearchEngine:
    """TRUSTGATE_SEARCH=scripted:flag_a,flag_b picks ScriptedSearch; unset means unavailable."""
    value = (os.environ if env is None else env).get("TRUSTGATE_SEARCH", "")
    if value.startswith("scripted:"):
        return ScriptedSearch([f.strip() for f in value[len("scripted:"):].split(",")
                               if f.strip()])
    return UnavailableSearch()
