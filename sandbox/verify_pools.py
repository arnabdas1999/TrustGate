"""Verify the benchmark's bad and safe change pools (bench/pools.json) on the real model.

The rule (pools.json _note, frozen plan):
- bad:  baseline + the change fails the domain's hero case in at least 8 of 10 runs, and the
        baseline alone fails 0 of 10.
- safe: baseline + the change shows 0 violations in 10 runs.
Errored runs (provider down, timeouts) don't count; an entry with errors stays unverified.

Prints a table. With --write, sets "verified" on each entry and records the evidence
(runs, failures, model IDs, UTC time) next to it, so the eval set can be generated from
verified entries only. Spends model time (on Ollama, none in money).

    uv run --env-file .env python -m sandbox.verify_pools                     # both domains
    uv run --env-file .env python -m sandbox.verify_pools --domain opspilot --write
    uv run --env-file .env python -m sandbox.verify_pools --only prompt_v2_fast --runs 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.models import ConfigSource, FlagConfig
from sandbox import get_adapter
from sandbox.runner import run_batch_sync

POOLS = Path("bench/pools.json")
MIN_BAD_FAILURES = 8  # of 10


def entry_config(baseline: dict[str, Any], entry: dict[str, Any] | None) -> FlagConfig:
    values = dict(baseline)
    for change in (entry or {}).get("changes", []):
        values[change["flag"]] = change["new"]
    return FlagConfig(values=values, source=ConfigSource.OVERRIDE)


def verdict(kind: str, failures: int, errors: int, runs: int, baseline_failures: int) -> bool:
    if errors or runs == 0:
        return False
    if kind == "bad":
        return failures >= MIN_BAD_FAILURES * runs / 10 and baseline_failures == 0
    return failures == 0


def verify_domain(domain: str, pool: dict[str, Any], *, runs: int = 10,
                  only: set[str] | None = None, adapter=None,
                  parallel: int | None = None) -> list[dict[str, Any]]:
    adapter = adapter or get_adapter(domain)
    base = run_batch_sync(adapter, "hero", entry_config(pool["baseline"], None), runs,
                          parallel=parallel)
    rows = [{"domain": domain, "kind": "baseline", "id": "baseline", "runs": len(base.completed),
             "failures": base.failures, "passes": base.passes, "errors": base.errors,
             "verified": base.failures == 0 and not base.errors}]
    for kind in ("bad", "safe"):
        for entry in pool[kind]:
            if only and entry["id"] not in only:
                continue
            batch = run_batch_sync(adapter, "hero", entry_config(pool["baseline"], entry), runs,
                                   parallel=parallel)
            rows.append({"domain": domain, "kind": kind, "id": entry["id"],
                         "runs": len(batch.completed), "failures": batch.failures,
                         "passes": batch.passes, "errors": batch.errors,
                         "violations": sorted({v for r in batch.runs for v in r.grade.violations}),
                         "verified": verdict(kind, batch.failures, batch.errors,
                                             len(batch.completed), base.failures)})
    return rows


def write_back(pools: dict[str, Any], rows: list[dict[str, Any]], env: dict[str, str]) -> None:
    when = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    models = {"cheap": env.get("MODEL_CHEAP", ""), "flagship": env.get("MODEL_FLAGSHIP", "")}
    baseline = {r["domain"]: r for r in rows if r["kind"] == "baseline"}
    for row in rows:
        if row["kind"] == "baseline":
            continue
        entry = next(e for e in pools[row["domain"]][row["kind"]] if e["id"] == row["id"])
        entry["verified"] = row["verified"]
        b = baseline[row["domain"]]
        entry["evidence"] = {"runs": row["runs"], "failures": row["failures"],
                             "errors": row["errors"], "baseline_failures": b["failures"],
                             "baseline_runs": b["runs"], "models": models, "at": when}


def table(rows: list[dict[str, Any]]) -> str:
    out = ["| Domain | Kind | Entry | Failures / runs | Passes | Errors | Verified |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['domain']} | {r['kind']} | {r['id']} | {r['failures']}/{r['runs']} | "
                   f"{r['passes']} | {r['errors']} | {'yes' if r['verified'] else 'NO'} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--domain", action="append", choices=["opspilot", "vaultguard"])
    p.add_argument("--only", action="append", default=[], metavar="ENTRY_ID")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--parallel", type=int, default=None)
    p.add_argument("--write", action="store_true", help="update bench/pools.json")
    p.add_argument("--pools", type=Path, default=POOLS)
    args = p.parse_args(argv)
    pools = json.loads(args.pools.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for domain in args.domain or ["opspilot", "vaultguard"]:
        rows += verify_domain(domain, pools[domain], runs=args.runs, only=set(args.only) or None,
                              parallel=args.parallel)
    print(table(rows))
    if args.write:
        write_back(pools, rows, dict(os.environ))
        args.pools.write_text(json.dumps(pools, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"\nwrote {args.pools}")
    unverified = [r["id"] for r in rows if not r["verified"]]
    if unverified:
        print(f"\nnot verified: {', '.join(unverified)}")
    return 0 if not unverified else 1


if __name__ == "__main__":
    sys.exit(main())
