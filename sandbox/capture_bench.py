"""Capture a benchmark case: replay bench/releases/<case>.json in the sandbox and write the
incident as bench/incidents/<case>.json, the name bench/run_arms.py looks for.

The release's flag changes are applied on top of the baseline (a sandbox stand-in for the
team shipping that release; Flagsmith isn't touched). The domain's hero case is run until a
run violates something, up to --attempts; that run becomes the incident. Baseline and release
snapshots are written next to it as {"values": ...} files and referenced as file: paths, so the
orchestrator's release diff gives exactly the release's changes. Spends credits.

    uv run --env-file .env python -m sandbox.capture_bench --case opspilot-dev-1
    uv run --env-file .env python -m sandbox.capture_bench --all --set dev
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from contracts.models import ConfigSource, FlagConfig, Incident
from sandbox import get_adapter
from sandbox.flags import BASELINE
from sandbox.live import INCIDENTS_DIR, capture

RELEASES_DIR = Path("bench/releases")


class CaptureError(Exception):
    pass


def release_values(release: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(baseline values, release values) for a bench release file."""
    baseline = dict(BASELINE)
    for change in release.get("changes", []):
        if change.get("old") is not None:
            baseline[change["flag"]] = change["old"]
    values = dict(baseline)
    for change in release.get("changes", []):
        values[change["flag"]] = change["new"]
    return baseline, values


def capture_case(case: str, *, attempts: int = 5, releases_dir: Path = RELEASES_DIR,
                 out_dir: Path = INCIDENTS_DIR, adapter=None) -> Incident:
    path = releases_dir / f"{case}.json"
    if not path.exists():
        raise CaptureError(f"no release at {path} (bench.make_incident writes it)")
    release = json.loads(path.read_text(encoding="utf-8"))
    adapter = adapter or get_adapter(release["domain"])
    baseline, values = release_values(release)

    out_dir.mkdir(parents=True, exist_ok=True)
    base_path, rel_path = out_dir / f"{case}.baseline.json", out_dir / f"{case}.release.json"
    base_path.write_text(json.dumps({"values": baseline}, indent=2), encoding="utf-8")
    rel_path.write_text(json.dumps({"values": values}, indent=2), encoding="utf-8")

    resolved = FlagConfig(values=values, source=ConfigSource.CAPTURED)
    identity = adapter.identity("hero")
    errors = []
    for seed in range(attempts):
        db = adapter.reset_state("hero")
        try:
            trace = adapter.run_agent("hero", resolved, identity, db, seed=seed)
            grade = adapter.grade(trace, db)
            if grade.violations:
                return capture(adapter, "hero", identity, resolved, trace, db,
                               baseline_snapshot=str(base_path),
                               release_snapshot=str(rel_path), out_dir=out_dir, name=case)
            if trace.error:
                errors.append(trace.error)
        finally:
            db.unlink(missing_ok=True)
    detail = f"; errors: {errors[:2]}" if errors else ""
    raise CaptureError(f"{case}: no violating run in {attempts} attempts "
                       f"(the release may not break the agent){detail}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--case", action="append", default=[])
    p.add_argument("--all", action="store_true", help="every release in bench/releases")
    p.add_argument("--set", default=None, help="with --all: only this set (dev or eval)")
    p.add_argument("--attempts", type=int, default=5)
    args = p.parse_args(argv)
    cases = list(args.case)
    if args.all:
        for path in sorted(RELEASES_DIR.glob("*.json")):
            release = json.loads(path.read_text(encoding="utf-8"))
            if args.set is None or release.get("set") == args.set:
                cases.append(release["case"])
    if not cases:
        p.error("give --case NAME or --all")
    failed = 0
    for case in cases:
        try:
            incident = capture_case(case, attempts=args.attempts)
            print(f"{case}: captured ({incident.trace.reported_status}, "
                  f"{[c.name for c in incident.trace.tool_calls]})")
        except CaptureError as exc:
            failed += 1
            print(f"{case}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
