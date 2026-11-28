"""Scaling test: how many replays does search need as a release grows from 5 to 10 to 20 changes?

Each release is the plan's 5 real changes plus harmless flags the agents read (sandbox/flags.py
HARMLESS). Plain mode keeps diff order (by flag name), like the plain benchmark arm; guided
mode lets the investigator rank the suspects first (agent.ordering), like the guided arm.
Repairs aren't validated here: this measures the search (reproduce, ddmin, confirm).

    uv run python -m search.scaling                                  # synthetic oracle, free
    uv run python -m search.scaling --domain vaultguard --sizes 5,10,20
    uv run --env-file .env python -m search.scaling --oracle sandbox --sizes 5,10   # model time
    uv run --env-file .env python -m search.scaling --mode guided   # LLM orders the suspects

Exhaustive comparison: trying every combination of N changes takes 2^N configurations, 3 runs
each.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from typing import Any

from contracts.models import (
    FlagChange,
    FlagConfig,
    Incident,
    InvestigationMode,
    TestPhase,
)
from search.engine import SandboxOracle, SandboxSearch
from search.testing import Observation

REAL_CULPRITS = {"opspilot": {"agent_prompt_version"},
                 "vaultguard": {"agent_prompt_version", "agent_tool_policy"}}


def build_release(n: int, seed: int = 0) -> tuple[dict[str, Any], list[FlagChange]]:
    """(captured values, suspects in plain order) for a release with n changes (5 to 20)."""
    from sandbox.flags import BASELINE, HARMLESS, RELEASE_CHANGES

    if not len(RELEASE_CHANGES) <= n <= len(RELEASE_CHANGES) + len(HARMLESS):
        raise ValueError(f"n must be {len(RELEASE_CHANGES)} to "
                         f"{len(RELEASE_CHANGES) + len(HARMLESS)}")
    extra = random.Random(seed).sample(sorted(HARMLESS), n - len(RELEASE_CHANGES))  # noqa: S311
    changes = [FlagChange(flag=f, old=BASELINE[f], new=v) for f, v in RELEASE_CHANGES.items()]
    changes += [FlagChange(flag=f, old=HARMLESS[f][0], new=HARMLESS[f][1]) for f in extra]
    captured = {**BASELINE, **{c.flag: c.new for c in changes}}
    return captured, sorted(changes, key=lambda c: c.flag)


def make_incident(domain: str, captured: dict[str, Any], identity: dict[str, Any]) -> Incident:
    trace = {"run_id": "scaling", "domain": domain, "case_id": "hero",
             "flags": {"values": captured, "source": "captured"}, "identity": identity,
             "model": "n/a"}
    return Incident(id=f"scaling-{domain}", domain=domain, case_id="hero", identity=identity,
                    resolved_flags={"values": captured, "source": "captured"},
                    baseline_snapshot="n/a", release_snapshot="n/a", db_snapshot="n/a",
                    trace=trace, created_at=datetime.now(UTC))


def synthetic_oracle(domain: str):
    """Deterministic stand-in for the agent: fails exactly when the domain's real culprit
    changes are all present. Costs nothing; counts are what the real search would spend on a
    perfectly reproducible failure (a lower bound)."""
    culprits = REAL_CULPRITS[domain]
    from sandbox.flags import RELEASE

    def oracle(config: FlagConfig, n: int, *, early_stop: bool, case_id=None) -> Observation:
        bad = all(config.values.get(f) == RELEASE[f] for f in culprits)
        failures = (1 if early_stop else n) if bad else 0
        runs = failures if early_stop and failures else n
        return Observation(runs=runs, failures=failures, passes=runs - failures)
    return oracle


def guided_order(suspects: list[FlagChange], trace=None, llm=None, env=None
                 ) -> tuple[list[FlagChange], str]:
    """The investigator's suspect ranking (agent.ordering, Naman's), as the guided arm uses it.
    Falls back to diff order, and says so, when the LLM is unavailable."""
    from agent.llm import investigator_model, llm_from_env
    from agent.ordering import order_suspects

    llm = llm if llm is not None else llm_from_env(env)
    model = investigator_model(env)  # the same ranking model as the orchestrator's guided arm
    if not model:
        raise ValueError("no investigator model configured (set MODEL_FLAGSHIP)")
    ordering = order_suspects(suspects, trace, InvestigationMode.GUIDED, llm, model=model)
    return list(ordering.suspects), ordering.source


def measure(domain: str, n: int, *, oracle: str = "synthetic", seed: int = 0,
            adapter=None, mode: str = "plain", llm=None, env=None) -> dict[str, Any]:
    captured, suspects = build_release(n, seed)
    order_source = "diff"
    if mode == "guided":
        suspects, order_source = guided_order(suspects, llm=llm, env=env)
    if oracle == "synthetic":
        identity = {"identifier": "scaling", "traits": {}}
        factory = lambda inc: synthetic_oracle(domain)  # noqa: E731
    else:
        from sandbox import get_adapter

        adapter = adapter or get_adapter(domain)
        identity = adapter.identity("hero").model_dump()
        factory = lambda inc: SandboxOracle(adapter, inc)  # noqa: E731
    engine = SandboxSearch(factory, heldout_cases=lambda inc: [], max_runs=10_000,
                           max_usd=50.0, validate_repairs=False)
    result = engine.run(make_incident(domain, captured, identity), suspects,
                        InvestigationMode.GUIDED if mode == "guided" else InvestigationMode.PLAIN)

    def runs(phase: TestPhase) -> int:
        return sum(s.runs for s in result.steps if s.phase is phase)

    search_tests = sum(1 for s in result.steps if s.phase is TestPhase.SEARCH)
    return {"domain": domain, "changes": n, "mode": mode, "order_source": order_source,
            "outcome": result.outcome.value,
            "culprit": sorted(c.flag for c in result.culprit_set),
            "search_configs": search_tests, "search_runs": runs(TestPhase.SEARCH),
            "confirm_runs": runs(TestPhase.CONFIRM),
            "reproduce_runs": runs(TestPhase.REPRODUCE), "total_runs": result.runs_used,
            "usd": round(result.usd_used, 4), "exhaustive_configs": 2 ** n,
            "exhaustive_runs": 3 * 2 ** n}


def table(rows: list[dict[str, Any]]) -> str:
    head = ("| Changes | Culprit | Configs tested | Search runs | Total runs (incl. reproduce "
            "+ confirm) | Exhaustive (2^N x 3) |\n|---|---|---|---|---|---|")
    lines = [head]
    for r in rows:
        lines.append(f"| {r['changes']} | {', '.join(r['culprit']) or r['outcome']} | "
                     f"{r['search_configs']} | {r['search_runs']} | {r['total_runs']} | "
                     f"{r['exhaustive_runs']:,} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--domain", default="opspilot", choices=sorted(REAL_CULPRITS))
    p.add_argument("--sizes", default="5,10,20")
    p.add_argument("--oracle", default="synthetic", choices=["synthetic", "sandbox"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--mode", default="plain", choices=["plain", "guided"])
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    rows = [measure(args.domain, int(n), oracle=args.oracle, seed=args.seed, mode=args.mode)
            for n in args.sizes.split(",") if n.strip()]
    sources = sorted({r["order_source"] for r in rows})
    print(f"Scaling test: {args.domain}, {args.oracle} oracle, {args.mode} order "
          f"(source: {', '.join(sources)}), seed {args.seed}\n")
    print(table(rows))
    if args.json:
        print(json.dumps(rows, indent=2))
    return 0 if all(r["outcome"] == "found" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
