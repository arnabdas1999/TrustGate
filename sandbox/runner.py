"""Parallel replays: N runs of one configuration on one case, each on a fresh database copy.

- 8 to 16 runs in flight (asyncio + threads; the provider retries 429s with backoff), 2 by
  default for a local Ollama model; SANDBOX_PARALLEL overrides.
- early_stop: stop launching runs after the first violation (search phases). Reproduction and
  validation pass early_stop=False to get full counts.
- A run whose agent errored (provider down, timeout) before breaking any rule is counted in
  `errors`, never as a pass. A run that broke a rule and then errored still counts as a
  failure: the violation happened (the grader reads the database, not the transcript).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from contracts.models import FlagConfig, GradeResult, Identity, Trace
from sandbox import llm
from sandbox.adapter import TargetAppAdapter


@dataclass
class Run:
    trace: Trace
    grade: GradeResult
    seed: int


@dataclass
class Batch:
    case_id: str
    config: FlagConfig
    requested: int
    runs: list[Run] = field(default_factory=list)

    @property
    def completed(self) -> list[Run]:
        """Runs with a verdict: finished, or errored after a violation already happened."""
        return [r for r in self.runs if not r.trace.error or r.grade.violations]

    @property
    def failures(self) -> int:
        return sum(1 for r in self.completed if r.grade.violations)

    @property
    def passes(self) -> int:
        return sum(1 for r in self.completed if r.grade.passed)

    @property
    def errors(self) -> int:
        return len(self.runs) - len(self.completed)

    @property
    def cost_usd(self) -> float:
        return round(sum(r.trace.cost_usd for r in self.runs), 6)

    @property
    def mean_duration_s(self) -> float:
        done = self.completed
        return round(sum(r.trace.duration_s for r in done) / len(done), 2) if done else 0.0


def default_parallelism() -> int:
    """SANDBOX_PARALLEL, else 8; 2 for a local Ollama model, which serves only a few requests
    at once (more just queue there and run into the per-run timeout)."""
    default = "2" if llm.provider_name() == "ollama" else "8"
    return max(1, min(16, int(os.environ.get("SANDBOX_PARALLEL", "") or default)))


def _one(adapter: TargetAppAdapter, case_id: str, config: FlagConfig, identity: Identity,
         seed: int, keep_db: bool) -> Run:
    db: Path = adapter.reset_state(case_id, 0)  # same starting state; seed varies the model
    try:
        trace = adapter.run_agent(case_id, config, identity, db, seed=seed)
        return Run(trace=trace, grade=adapter.grade(trace, db), seed=seed)
    finally:
        if not keep_db:
            db.unlink(missing_ok=True)


async def run_batch(adapter: TargetAppAdapter, case_id: str, config: FlagConfig, n: int, *,
                    identity: Identity | None = None, early_stop: bool = False,
                    parallel: int | None = None, seed_base: int = 0,
                    keep_db: bool = False) -> Batch:
    identity = identity or adapter.identity(case_id)
    batch = Batch(case_id=case_id, config=config, requested=n)
    sem = asyncio.Semaphore(parallel or default_parallelism())
    stop = asyncio.Event()

    async def worker(i: int) -> None:
        async with sem:
            if stop.is_set():
                return
            run = await asyncio.to_thread(_one, adapter, case_id, config, identity,
                                          seed_base + i, keep_db)
            batch.runs.append(run)
            if early_stop and run.grade.violations:
                stop.set()

    await asyncio.gather(*(worker(i) for i in range(n)))
    batch.runs.sort(key=lambda r: r.seed)
    return batch


def run_batch_sync(adapter: TargetAppAdapter, case_id: str, config: FlagConfig, n: int,
                   **kwargs) -> Batch:
    return asyncio.run(run_batch(adapter, case_id, config, n, **kwargs))
