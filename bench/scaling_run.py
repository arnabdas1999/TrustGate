"""Run the scaling test and append one ScalingRecord per search to bench/records/scaling.jsonl,
the file bench/results.py reads. The search itself is Khyati's search.scaling.measure; this only
records what it returns.

    uv run --env-file .env python -m bench.scaling_run --oracle sandbox             # all 12
    uv run --env-file .env python -m bench.scaling_run --oracle sandbox --domain vaultguard \\
        --modes guided --sizes 20

Default: both domains, sizes 5, 10 and 20, plain then guided (12 searches). Each search prints
its row as it finishes and is appended at once, so a stopped run keeps what it measured.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from bench.records import SCALING_FILE, Arm, ScalingRecord, append

DOMAINS = ("opspilot", "vaultguard")
MODES = ("plain", "guided")


def to_record(row: dict[str, Any], culprits: set[str]) -> ScalingRecord:
    return ScalingRecord(
        case=f"scaling-{row['domain']}-{row['changes']}",
        release_size=row["changes"],
        arm=Arm(row["mode"]),
        search_runs=row["search_runs"],
        found_culprit=row["outcome"] == "found" and set(row["culprit"]) == culprits,
    )


def run(domains: Sequence[str], sizes: Sequence[int], modes: Sequence[str], *, oracle: str,
        seed: int, out: Path, measure: Callable[..., dict[str, Any]] | None = None,
        culprits: dict[str, set[str]] | None = None,
        emit: Callable[[str], None] = print) -> list[ScalingRecord]:
    if measure is None or culprits is None:
        from search.scaling import REAL_CULPRITS
        from search.scaling import measure as real_measure

        measure = measure or real_measure
        culprits = culprits or REAL_CULPRITS
    records = []
    for domain in domains:
        for mode in modes:
            for n in sizes:
                row = measure(domain, n, oracle=oracle, seed=seed, mode=mode)
                record = to_record(row, culprits[domain])
                append(out, record)
                records.append(record)
                emit(f"{domain} {mode} {n} changes: {record.search_runs} search runs, "
                     f"culprit {'found' if record.found_culprit else 'NOT found'} "
                     f"(order: {row['order_source']})")
    return records


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--domain", action="append", choices=DOMAINS,
                   help="repeatable; default both")
    p.add_argument("--sizes", default="5,10,20")
    p.add_argument("--modes", default="plain,guided")
    p.add_argument("--oracle", default="sandbox", choices=["synthetic", "sandbox"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=SCALING_FILE)
    args = p.parse_args(argv)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if not set(modes) <= set(MODES):
        p.error(f"--modes takes {', '.join(MODES)}")
    sizes = [int(n) for n in args.sizes.split(",") if n.strip()]
    records = run(args.domain or list(DOMAINS), sizes, modes, oracle=args.oracle,
                  seed=args.seed, out=args.out)
    print(f"appended {len(records)} records to {args.out}")
    return 0 if all(r.found_culprit for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
