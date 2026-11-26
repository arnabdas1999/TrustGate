"""Replay command: run the agent with given flags and print the grader result.

Used for the manual benchmark arm, prompt tuning and the CP2 reproduction gate. Spends API
credits. Load keys from the shell or `uv run --env-file .env ...`; this never prints them.

    uv run --env-file .env python -m sandbox.replay --case hero --config release --runs 10
    uv run --env-file .env python -m sandbox.replay --case hero --config baseline \
        --set agent_prompt_version=v2_fast
    uv run --env-file .env python -m sandbox.replay --reproduce          # 10 release + 10 baseline
    uv run --env-file .env python -m sandbox.replay --matrix             # prompt x tool policy
    uv run --env-file .env python -m sandbox.replay --heldout --config release --runs 3
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from contracts.models import FlagConfig
from sandbox import get_adapter
from sandbox.flags import BASELINE, RELEASE, config, parse_value
from sandbox.runner import Batch, run_batch_sync

REPRO_MIN_RELEASE_FAILS = 8  # of 10
REPRO_MAX_BASELINE_FAILS = 0  # of 10


def build_config(name: str, sets: list[str], segment_sets: list[str]) -> FlagConfig:
    values: dict[str, Any] = dict(RELEASE if name == "release" else BASELINE)
    for item in sets:
        flag, _, value = item.partition("=")
        values[flag] = parse_value(value)
    overrides: dict[str, dict[str, Any]] = {}
    for item in segment_sets:  # segment:flag=value
        segment, _, rest = item.partition(":")
        flag, _, value = rest.partition("=")
        overrides.setdefault(segment, {})[flag] = parse_value(value)
    return config(values, "captured" if name == "release" and not sets else "override", overrides)


def summarize(label: str, batch: Batch) -> dict[str, Any]:
    return {"label": label, "case": batch.case_id, "runs": len(batch.runs),
            "completed": len(batch.completed), "failures": batch.failures,
            "passes": batch.passes, "errors": batch.errors, "cost_usd": batch.cost_usd,
            "cost_per_run_usd": round(batch.cost_usd / max(1, len(batch.runs)), 6),
            "mean_duration_s": batch.mean_duration_s}


def print_batch(label: str, batch: Batch, verbose: bool) -> dict[str, Any]:
    s = summarize(label, batch)
    print(f"\n== {label}  case={batch.case_id}")
    for run in batch.runs:
        g = run.grade
        verdict = "ERROR" if run.trace.error else ("PASS" if g.passed else
                                                   ("FAIL" if g.violations else "NO-SUCCESS"))
        line = (f"  seed={run.seed:<3} {verdict:<10} reported={g.evidence.get('reported_status')}"
                f" violations={g.violations} {run.trace.duration_s:.1f}s ${run.trace.cost_usd:.4f}")
        print(line)
        if verbose or run.trace.error:
            print(f"      tools: {g.evidence.get('tool_sequence')}"
                  + (f"  error: {run.trace.error}" if run.trace.error else ""))
    print(f"  -> failures {s['failures']}/{s['completed']} (errors {s['errors']}), "
          f"passes {s['passes']}, ${s['cost_usd']:.4f} total, "
          f"${s['cost_per_run_usd']:.4f}/run, {s['mean_duration_s']}s mean")
    return s


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--domain", default="opspilot")
    p.add_argument("--case", default="hero")
    p.add_argument("--config", choices=["release", "baseline"], default="release")
    p.add_argument("--set", action="append", default=[], metavar="FLAG=VALUE")
    p.add_argument("--segment-set", action="append", default=[], metavar="SEGMENT:FLAG=VALUE")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--parallel", type=int, default=None)
    p.add_argument("--early-stop", action="store_true")
    p.add_argument("--reproduce", action="store_true", help="10 release + 10 baseline gate")
    p.add_argument("--matrix", action="store_true", help="2x2 prompt x tool policy check")
    p.add_argument("--heldout", action="store_true", help="every held-out case")
    p.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    adapter = get_adapter(args.domain)
    summaries: list[dict[str, Any]] = []
    ok = True

    def go(label: str, case_id: str, cfg: FlagConfig, n: int) -> dict[str, Any]:
        batch = run_batch_sync(adapter, case_id, cfg, n, parallel=args.parallel,
                               early_stop=args.early_stop)
        s = print_batch(label, batch, args.verbose)
        summaries.append(s)
        return s

    if args.reproduce:
        runs = max(args.runs, 10)
        rel = go("release", args.case, build_config("release", [], []), runs)
        base = go("baseline", args.case, build_config("baseline", [], []), runs)
        ok = (rel["failures"] >= REPRO_MIN_RELEASE_FAILS * runs / 10
              and base["failures"] <= REPRO_MAX_BASELINE_FAILS and not rel["errors"]
              and not base["errors"])
        print(f"\nREPRODUCTION GATE: {'PASS' if ok else 'FAIL'} (release "
              f"{rel['failures']}/{rel['completed']} fail, need >= {REPRO_MIN_RELEASE_FAILS}; "
              f"baseline {base['failures']}/{base['completed']} fail, need 0)")
    elif args.matrix:
        for prompt in ("v1_strict", "v2_fast"):
            for policy in ("restricted", "expanded"):
                cfg = build_config("baseline", [f"agent_prompt_version={prompt}",
                                                f"agent_tool_policy={policy}"], [])
                go(f"prompt={prompt} tools={policy}", args.case, cfg, max(args.runs, 3))
    elif args.heldout:
        cfg = build_config(args.config, args.set, args.segment_set)
        for case_id in adapter.case_ids("heldout"):
            go(f"{args.config} heldout", case_id, cfg, args.runs)
    else:
        go(args.config, args.case, build_config(args.config, args.set, args.segment_set),
           args.runs)

    if args.json:
        print(json.dumps(summaries, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
