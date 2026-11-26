"""Generate bench/results.md from benchmark records. Never type numbers into the table.

    uv run python -m bench.results [--runs PATH] [--manual PATH] [--scaling PATH]
                                   [--keys DIR] [--out bench/results.md]

Inputs:
  runs.jsonl       RunRecords from run_arms.py (global_rollback, plain, guided)
  manual_log.csv   the timer's log of the manual arm, converted to RunRecords here
  scaling.jsonl    ScalingRecords (search runs by release size)
  bench/keys/      sealed answer keys; attribution rows stay "pending" until they exist

summarize() and render() are pure, so tests call them directly.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bench.records import (
    MANUAL_LOG,
    MANUAL_LOG_COLUMNS,
    RUNS_FILE,
    SCALING_FILE,
    Arm,
    RunRecord,
    ScalingRecord,
    read,
)

BENCH_DIR = Path(__file__).resolve().parent
KEYS_DIR = BENCH_DIR / "keys"
RESULTS_FILE = BENCH_DIR / "results.md"

ARM_ORDER = [Arm.MANUAL, Arm.GLOBAL_ROLLBACK, Arm.PLAIN, Arm.GUIDED]
ARM_LABELS = {
    Arm.MANUAL: "Manual",
    Arm.GLOBAL_ROLLBACK: "Global rollback",
    Arm.PLAIN: "Plain ddmin",
    Arm.GUIDED: "TrustGate",
}
SCALING_ARMS = [Arm.PLAIN, Arm.GUIDED]
SCALING_SIZES = [5, 10, 20]
EMPTY = "—"
PENDING = "pending"

Key = dict[str, Any]


class ManualLogError(ValueError):
    """A row of manual_log.csv could not be converted. The message names the line."""


# ---------------------------------------------------------------- loading


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def _parse_time(value: str, column: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{column} is not an ISO 8601 time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{column} needs a timezone offset: {value!r}")
    return parsed


def manual_row_to_record(row: dict[str, str]) -> RunRecord:
    """Convert one manual_log.csv row (column -> stripped text) into a manual-arm RunRecord."""
    case = row["case"]
    if not case:
        raise ValueError("case is empty")
    if not row["started_at"]:
        raise ValueError("started_at is empty")
    started = _parse_time(row["started_at"], "started_at")
    first = _parse_time(row["first_suspect_at"], "first_suspect_at") if row[
        "first_suspect_at"] else None
    done = _parse_time(row["done_at"], "done_at") if row["done_at"] else None

    unsolved_text = row["unsolved"].lower()
    if unsolved_text not in ("yes", "no"):
        raise ValueError(f"unsolved must be yes or no, got {row['unsolved']!r}")
    unsolved = unsolved_text == "yes"
    if not unsolved and done is None:
        raise ValueError("done_at is empty but unsolved is no")

    active_ms = None
    if done is not None:
        active_ms = round((done - started).total_seconds() * 1000)
        if active_ms < 0:
            raise ValueError("done_at is before started_at")

    replays = row["replays_run"]
    if replays and not replays.isdigit():
        raise ValueError(f"replays_run must be a whole number, got {replays!r}")

    notes = [f"final change: {row['final_change']}"] if row["final_change"] else []
    if row["notes"]:
        notes.append(row["notes"])

    return RunRecord(
        case=case,
        domain=case.split("-", 1)[0],
        arm=Arm.MANUAL,
        person=row["person"] or None,
        started_at=started,
        first_suspect_at=first,
        applied_at=None if unsolved else done,
        unsolved=unsolved,
        human_active_ms=active_ms,  # manual arm: active time is the whole wall clock
        diagnostic_runs=int(replays) if replays else None,
        reported_culprit=_split_list(row["final_suspect"]),
        flags_reverted=_split_list(row["flags_reverted"]),
        notes="; ".join(notes),
    )


def load_manual_log(path: Path) -> list[RunRecord]:
    """Read manual_log.csv. Blank rows are skipped; a malformed row raises ManualLogError."""
    if not path.exists():
        return []
    records: list[RunRecord] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return []
        if [h.strip() for h in header] != MANUAL_LOG_COLUMNS:
            raise ManualLogError(
                f"{path}:1: header must be {','.join(MANUAL_LOG_COLUMNS)}"
            )
        for cells in reader:
            line = reader.line_num
            if not any(c.strip() for c in cells):
                continue
            if len(cells) != len(MANUAL_LOG_COLUMNS):
                raise ManualLogError(
                    f"{path}:{line}: expected {len(MANUAL_LOG_COLUMNS)} columns, "
                    f"got {len(cells)}"
                )
            row = {col: cell.strip() for col, cell in zip(MANUAL_LOG_COLUMNS, cells, strict=True)}
            try:
                records.append(manual_row_to_record(row))
            except (ValueError, ValidationError) as exc:
                raise ManualLogError(f"{path}:{line}: {exc}") from exc
    return records


def load_keys(keys_dir: Path) -> dict[str, Key]:
    """Answer keys by case name (the key file's stem). Empty while keys are sealed."""
    if not keys_dir.is_dir():
        return {}
    keys: dict[str, Key] = {}
    for path in sorted(keys_dir.glob("*.json")):
        key = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(key, dict) and "bad_changes" in key:
            keys[path.stem] = key
    return keys


def load_runs(runs_path: Path, manual_path: Path) -> list[RunRecord]:
    return list(read(runs_path, RunRecord)) + load_manual_log(manual_path)


# ---------------------------------------------------------------- calculation


def bad_flags(key: Key) -> set[str]:
    return {change["flag"] for change in key["bad_changes"]}


def is_correct(run: RunRecord, key: Key) -> bool:
    return set(run.reported_culprit) == bad_flags(key)


def unnecessary_reverts(run: RunRecord, key: Key) -> int:
    bad = bad_flags(key)
    return sum(1 for flag in run.flags_reverted if flag not in bad)


def _ms(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() * 1000


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize_arm(runs: Sequence[RunRecord], keys: dict[str, Key]) -> dict[str, Any]:
    """Numbers for one arm. Time medians use solved runs only; unsolved runs still count in n."""
    solved = [r for r in runs if not r.unsolved]
    keyed = [(r, keys[r.case]) for r in runs if r.case in keys]
    keyed_solved = [(r, k) for r, k in keyed if not r.unsolved]
    interaction = [(r, k) for r, k in keyed if len(k["bad_changes"]) > 1]
    applied_ms = [_ms(r.started_at, r.applied_at) for r in solved if r.applied_at is not None]
    fresh = [r.fresh_validation_failed for r in runs if r.fresh_validation_failed is not None]
    costs = [r.inference_usd for r in runs if r.inference_usd is not None]

    return {
        "n": len(runs),
        "unsolved": len(runs) - len(solved),
        "keyed": len(keyed),
        "human_active_ms_median": _median(
            [r.human_active_ms for r in solved if r.human_active_ms is not None]
        ),
        "cost_kept_median": _median([r.cost_kept for r in runs if r.cost_kept is not None]),
        "unnecessary_reverts_mean": (
            statistics.fmean(unnecessary_reverts(r, k) for r, k in keyed_solved)
            if keyed_solved else None
        ),
        "interaction_found": sum(1 for r, k in interaction if is_correct(r, k)),
        "interaction_total": len(interaction),
        "fresh_failed": sum(fresh),
        "fresh_with_data": len(fresh),
        "first_correct_suspect_ms_median": _median([
            _ms(r.started_at, r.first_suspect_at)
            for r, k in keyed_solved
            if r.first_suspect_at is not None and is_correct(r, k)
        ]),
        "applied_ms_median": _median(applied_ms),
        "applied_ms_min": min(applied_ms) if applied_ms else None,
        "applied_ms_max": max(applied_ms) if applied_ms else None,
        "correct": sum(1 for r, k in keyed if is_correct(r, k)),
        "diagnostic_runs_median": _median(
            [r.diagnostic_runs for r in runs if r.diagnostic_runs is not None]
        ),
        "inference_usd_total": sum(costs) if costs else None,
    }


def summarize(runs: Iterable[RunRecord], keys: dict[str, Key]) -> dict[str, Any]:
    runs = list(runs)
    arms = {
        arm.value: summarize_arm([r for r in runs if r.arm == arm], keys) for arm in ARM_ORDER
    }
    return {
        "arms": arms,
        "keys_present": bool(keys),
        "total_runs": len(runs),
        "keyed_runs": sum(1 for r in runs if r.case in keys),
    }


def summarize_scaling(records: Iterable[ScalingRecord]) -> dict[int, dict[str, dict[str, Any]]]:
    """release size -> arm -> {"median": search_runs median or None, "n": count}."""
    records = list(records)
    table: dict[int, dict[str, dict[str, Any]]] = {}
    for size in SCALING_SIZES:
        table[size] = {}
        for arm in SCALING_ARMS:
            values = [r.search_runs for r in records if r.release_size == size and r.arm == arm]
            table[size][arm.value] = {"median": _median(values), "n": len(values)}
    return table


# ---------------------------------------------------------------- formatting


def fmt_duration(ms: float | None) -> str:
    if ms is None:
        return EMPTY
    seconds = round(ms / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def fmt_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return EMPTY
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.{digits}f}"


def _cells(summary: dict[str, Any]) -> list[tuple[str, list[str]]]:
    keys_present = summary["keys_present"]
    rows: list[tuple[str, list[str]]] = []

    def row(label: str, fn: Any, needs_keys: bool = False) -> None:
        cells = []
        for arm in ARM_ORDER:
            s = summary["arms"][arm.value]
            if s["n"] == 0:
                cells.append(EMPTY)
            elif needs_keys and not keys_present:
                cells.append(PENDING)
            elif needs_keys and s["keyed"] == 0:
                cells.append(EMPTY)
            else:
                cells.append(fn(s))
        rows.append((label, cells))

    def applied(s: dict[str, Any]) -> str:
        if s["applied_ms_median"] is None:
            return EMPTY
        span = f"{fmt_duration(s['applied_ms_min'])}–{fmt_duration(s['applied_ms_max'])}"
        return f"{fmt_duration(s['applied_ms_median'])} ({span})"

    row("Human active time (median)", lambda s: fmt_duration(s["human_active_ms_median"]))
    row("Cost saving kept vs release (median)", lambda s: (
        EMPTY if s["cost_kept_median"] is None else f"{s['cost_kept_median'] * 100:.0f}%"))
    row("Flags reverted unnecessarily (mean)",
        lambda s: fmt_number(s["unnecessary_reverts_mean"]), needs_keys=True)
    row("Interaction cases found", lambda s: (
        EMPTY if s["interaction_total"] == 0
        else f"{s['interaction_found']} of {s['interaction_total']}"), needs_keys=True)
    row("Final fixes failing fresh validation", lambda s: (
        EMPTY if s["fresh_with_data"] == 0
        else f"{s['fresh_failed']} of {s['fresh_with_data']}"))
    row("Time to first suspect, correct runs (median)",
        lambda s: fmt_duration(s["first_correct_suspect_ms_median"]), needs_keys=True)
    row("Median time to applied fix (range)", applied)
    row("Correct attribution", lambda s: f"{s['correct']} of {s['keyed']}", needs_keys=True)
    row("Diagnostic runs (median)", lambda s: fmt_number(s["diagnostic_runs_median"]))
    row("Inference cost (total)", lambda s: (
        EMPTY if s["inference_usd_total"] is None else f"${s['inference_usd_total']:.2f}"))
    return rows


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


def _n_phrase(counts: list[int]) -> str:
    present = [c for c in counts if c > 0] or [0]
    low, high = min(present), max(present)
    return str(low) if low == high else f"{low} to {high}"


def render(
    summary: dict[str, Any],
    scaling: Iterable[ScalingRecord] | dict[int, dict[str, dict[str, Any]]],
    generated_at: datetime | None = None,
) -> str:
    when = (generated_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    arms = summary["arms"]
    counts = [arms[a.value]["n"] for a in ARM_ORDER]
    scale = scaling if isinstance(scaling, dict) else summarize_scaling(scaling)

    out = [
        "# TrustGate benchmark results",
        "",
        f"Generated {when} from bench/records — do not edit by hand",
        "",
        "n per arm: " + " · ".join(
            f"{ARM_LABELS[a]} {arms[a.value]['n']}" for a in ARM_ORDER
        ),
        "",
        "## Results",
        "",
    ]
    out += _table(
        ["Metric"] + [ARM_LABELS[a] for a in ARM_ORDER],
        [[label, *cells] for label, cells in _cells(summary)],
    )

    out += ["", "## Scaling: search runs by release size", ""]
    scale_rows = []
    for size in SCALING_SIZES:
        cells = []
        for arm in SCALING_ARMS:
            cell = scale.get(size, {}).get(arm.value, {"median": None, "n": 0})
            cells.append(
                EMPTY if cell["n"] == 0 else f"{fmt_number(cell['median'])} (n={cell['n']})"
            )
        scale_rows.append([str(size), *cells])
    out += _table(["Release size"] + [ARM_LABELS[a] for a in SCALING_ARMS], scale_rows)
    out += ["", "Cell: median search runs (n = number of searches).", "", "## Notes", ""]

    unsolved = ", ".join(f"{ARM_LABELS[a]} {arms[a.value]['unsolved']}" for a in ARM_ORDER)
    out.append(
        f"- Unsolved runs count toward n but are excluded from time medians (unsolved: {unsolved})."
    )
    if not summary["keys_present"]:
        out.append(
            "- Attribution rows (unnecessary reverts, interaction cases, time to first correct "
            "suspect, correct attribution) are pending because the answer keys in bench/keys/ "
            "are still sealed."
        )
    elif summary["keyed_runs"] < summary["total_runs"]:
        out.append(
            f"- Attribution rows cover {summary['keyed_runs']} of {summary['total_runs']} runs; "
            "keys for the other cases are still sealed."
        )
    out.append(
        f"- n = {_n_phrase(counts)} per arm is a small demonstration, not grounds for broad "
        "statistical claims."
    )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=Path, default=RUNS_FILE)
    parser.add_argument("--manual", type=Path, default=MANUAL_LOG)
    parser.add_argument("--scaling", type=Path, default=SCALING_FILE)
    parser.add_argument("--keys", type=Path, default=KEYS_DIR)
    parser.add_argument("--out", type=Path, default=RESULTS_FILE)
    args = parser.parse_args(argv)

    try:
        runs = load_runs(args.runs, args.manual)
    except ManualLogError as exc:
        sys.exit(f"error: {exc}")
    summary = summarize(runs, load_keys(args.keys))
    markdown = render(summary, read(args.scaling, ScalingRecord))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
