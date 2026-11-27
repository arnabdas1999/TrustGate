"""The orchestrator's view of the search engine (Khyati's search/), behind a small protocol.

The plan's interface is `search.run(incident, suspects, mode)`. It reproduces the failure,
runs ddmin over the suspects, and validates repair candidates narrowest first. Until search/
lands on a branch, the orchestrator runs against:
- TRUSTGATE_SEARCH=sandbox: Khyati's search.SandboxSearch (real replays on the sandbox). It
  implements this protocol directly. Imported lazily; if search/ isn't importable the API
  still serves and investigations end INCONCLUSIVE with the reason.
- ScriptedSearch: deterministic, names configured culprits. For tests and the plumbing demo
  only, never for the benchmark (it doesn't replay anything).
- UnavailableSearch: the default. It refuses, so nothing pretends to have searched.

Suspects arrive already ordered (guided: agent/ordering.py; plain: diff order); the engine
takes the order as given.

Candidates carry no ops: code builds the WriteOps from the candidate's kind, the culprit set
and the segment (orchestrator/proposals.py).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    # When the search isolated (confirmed) the culprit, before repair validation. The
    # orchestrator stamps first_suspect with it, or with the time search.run returns.
    culprit_found_at: datetime | None = None


class SearchUnavailable(Exception):
    pass


class SearchEngine(Protocol):
    def run(self, incident: Incident, suspects: Sequence[FlagChange],
            mode: InvestigationMode) -> SearchResult: ...


log = logging.getLogger("trustgate.orchestrator")
DEFAULT_UNAVAILABLE = "no search engine configured (set TRUSTGATE_SEARCH)"


class UnavailableSearch:
    def __init__(self, reason: str = DEFAULT_UNAVAILABLE):
        self.reason = reason

    def run(self, incident: Incident, suspects: Sequence[FlagChange],
            mode: InvestigationMode) -> SearchResult:
        raise SearchUnavailable(self.reason)


def sandbox_search() -> SearchEngine:
    """Khyati's SandboxSearch, imported only when asked for. Never raises."""
    try:
        from search import SandboxSearch  # noqa: PLC0415 (lazy: search/ may not be installed)
    except Exception as exc:  # noqa: BLE001 (ImportError, or an error inside search/ itself)
        reason = f"TRUSTGATE_SEARCH=sandbox but search/ can't be imported: " \
                 f"{type(exc).__name__}: {exc}"
        log.warning(reason)
        return UnavailableSearch(reason)
    return SandboxSearch()


def _config(changes: Sequence[FlagChange]) -> FlagConfig:
    return FlagConfig(values={c.flag: c.new for c in changes if c.new is not None},
                      source=ConfigSource.OVERRIDE)


def _revert(values: Mapping[str, object], changes: Sequence[FlagChange]) -> dict:
    out = dict(values)
    for c in changes:
        if c.old is None:
            out.pop(c.flag, None)
        else:
            out[c.flag] = c.old
    return out


def _candidate_config(kind: RepairKind, captured: Mapping[str, object],
                      suspects: Sequence[FlagChange], culprit: Sequence[FlagChange],
                      segment: str | None) -> FlagConfig | None:
    """The configuration a candidate is validated under, shaped as search/repair.py does."""
    if kind is RepairKind.SEGMENT_OVERRIDE:
        if not segment:
            return None
        return FlagConfig(values=dict(captured), source=ConfigSource.OVERRIDE,
                          segment_overrides={segment: {c.flag: c.old for c in culprit}})
    changes = culprit if kind is RepairKind.SURGICAL else suspects
    return FlagConfig(values=_revert(captured, changes), source=ConfigSource.OVERRIDE)


class ScriptedSearch:
    """Deterministic stand-in: the configured flags are the culprits, every check is scripted.
    `segment` is the segment it validates the override in (the hero cases' tier0_production)."""

    def __init__(self, culprits: Sequence[str], segment: str | None = "tier0_production"):
        self.culprits = list(culprits)
        self.segment = segment

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
        captured = dict(incident.resolved_flags.values)
        scripted = {RepairKind.SEGMENT_OVERRIDE: (21, 1.0), RepairKind.SURGICAL: (20, 0.8),
                    RepairKind.GLOBAL: (18, 0.0)}
        candidates = []
        for kind, (held, kept) in scripted.items():
            cfg = _candidate_config(kind, captured, release, culprit, self.segment)
            if cfg is None:
                continue
            validation = TestResult(phase=TestPhase.VALIDATE, config=cfg, changes=release,
                                    runs=20, failures=0, outcome=TestOutcome.PASS)
            steps.append(validation)
            candidates.append(RepairCandidate(
                kind=kind, ops=[], validation=validation, heldout_passes=held,
                heldout_runs=21, baseline_heldout_passes=21, cost_kept=kept, violations=0))
        return SearchResult(outcome=SearchOutcome.FOUND, culprit_set=culprit, steps=steps,
                            candidates=candidates, runs_used=sum(s.runs for s in steps))


def sandbox_segments() -> Mapping[str, dict] | None:
    """The segment definitions the sandbox replays against (sandbox/flags.py SEGMENTS), or None
    if sandbox/ can't be imported."""
    try:
        from sandbox.flags import SEGMENTS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return SEGMENTS


def search_from_env(env: Mapping[str, str] | None = None) -> SearchEngine:
    """TRUSTGATE_SEARCH picks the engine:
    - sandbox: the real search (search.SandboxSearch: sandbox replays, ddmin, repair validation),
      imported lazily; if search/ can't be imported, UnavailableSearch with the reason
    - scripted:flag_a,flag_b[@segment]: ScriptedSearch for demos and tests (segment default
      tier0_production)
    - unset: unavailable (investigations end INCONCLUSIVE); any other value: unavailable with
      the reason."""
    value = (os.environ if env is None else env).get("TRUSTGATE_SEARCH", "").strip()
    if value == "sandbox":
        return sandbox_search()
    if value.startswith("scripted:"):
        flags, _, segment = value[len("scripted:"):].partition("@")
        return ScriptedSearch([f.strip() for f in flags.split(",") if f.strip()],
                              segment=segment.strip() or "tier0_production")
    if value:
        return UnavailableSearch(f"unknown TRUSTGATE_SEARCH value {value!r}")
    return UnavailableSearch()
