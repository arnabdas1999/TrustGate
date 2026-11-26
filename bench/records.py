"""Benchmark record formats shared by run_arms.py, results.py and the manual-arm log.

Records are JSON lines:
  bench/records/runs.jsonl     one RunRecord per (case, arm) attempt
  bench/records/scaling.jsonl  one ScalingRecord per scaling-test search
Manual runs are typed into bench/manual_log.csv (columns = MANUAL_LOG_COLUMNS) and
converted to RunRecords by results.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

RECORDS_DIR = Path(__file__).resolve().parent / "records"
RUNS_FILE = RECORDS_DIR / "runs.jsonl"
SCALING_FILE = RECORDS_DIR / "scaling.jsonl"
MANUAL_LOG = Path(__file__).resolve().parent / "manual_log.csv"


class Arm(StrEnum):
    MANUAL = "manual"
    GLOBAL_ROLLBACK = "global_rollback"
    PLAIN = "plain"  # plain ddmin, no LLM ordering
    GUIDED = "guided"  # TrustGate, LLM-guided


class RunRecord(BaseModel):
    case: str  # e.g. "opspilot-eval-3", matches bench/releases/<case>.json
    domain: str
    arm: Arm
    person: str | None = None  # who investigated (manual) or approved (plain, guided)
    started_at: datetime
    first_suspect_at: datetime | None = None
    applied_at: datetime | None = None  # repair applied in Flagsmith and incident replay passed
    unsolved: bool = False  # gave up, budget ran out, or INCONCLUSIVE
    human_active_ms: int | None = Field(default=None, ge=0)
    proposal_to_approve_ms: int | None = Field(default=None, ge=0)  # upper bound on attention
    diagnostic_runs: int | None = Field(default=None, ge=0)
    reported_culprit: list[str] = Field(default_factory=list)  # flag names the arm blamed
    validated_repair: bool | None = None
    fresh_validation_failed: bool | None = None  # final fix failed a fresh re-check
    flags_reverted: list[str] = Field(default_factory=list)  # flags the final fix reverted
    cost_kept: float | None = Field(default=None, ge=0.0, le=1.0)
    inference_usd: float | None = Field(default=None, ge=0.0)
    investigation_id: str | None = None
    notes: str = ""


class ScalingRecord(BaseModel):
    case: str
    release_size: int  # number of changed flags: 5, 10 or 20
    arm: Arm  # plain or guided
    search_runs: int = Field(ge=0)
    found_culprit: bool


# Header of bench/manual_log.csv, in order. Times are ISO 8601 with a timezone offset.
MANUAL_LOG_COLUMNS = [
    "case",
    "person",
    "started_at",
    "first_suspect_at",
    "done_at",
    "replays_run",
    "final_suspect",  # flag names separated by ';'
    "final_change",  # free text, e.g. "prompt reverted globally"
    "flags_reverted",  # flag names separated by ';'
    "unsolved",  # yes / no
    "notes",
]

def append(path: Path, record: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")


def read[M: BaseModel](path: Path, model: type[M]) -> Iterator[M]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield model.model_validate(json.loads(line))


def write_all(path: Path, records: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(record.model_dump_json() + "\n")
