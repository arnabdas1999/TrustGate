"""bench.scaling_run records what search.scaling.measure returns, one ScalingRecord per search."""

from __future__ import annotations

from bench.records import Arm, ScalingRecord, read
from bench.scaling_run import run, to_record

CULPRITS = {"opspilot": {"agent_prompt_version"},
            "vaultguard": {"agent_prompt_version", "agent_tool_policy"}}


def _row(domain, n, mode, *, culprit, outcome="found", runs=12):
    return {"domain": domain, "changes": n, "mode": mode, "order_source": "diff",
            "outcome": outcome, "culprit": culprit, "search_runs": runs}


def test_to_record_needs_the_whole_culprit_set():
    pair = ["agent_prompt_version", "agent_tool_policy"]
    found = to_record(_row("vaultguard", 10, "guided", culprit=pair), CULPRITS["vaultguard"])
    assert found == ScalingRecord(case="scaling-vaultguard-10", release_size=10, arm=Arm.GUIDED,
                                  search_runs=12, found_culprit=True)
    half = to_record(_row("vaultguard", 10, "plain", culprit=pair[:1]), CULPRITS["vaultguard"])
    assert half.found_culprit is False
    lost = to_record(_row("opspilot", 5, "plain", culprit=[], outcome="inconclusive"),
                     CULPRITS["opspilot"])
    assert lost.found_culprit is False


def test_run_appends_one_record_per_search(tmp_path):
    calls = []

    def measure(domain, n, *, oracle, seed, mode):
        calls.append((domain, mode, n, oracle))
        return _row(domain, n, mode, culprit=sorted(CULPRITS[domain]), runs=n)

    out = tmp_path / "scaling.jsonl"
    records = run(["opspilot", "vaultguard"], [5, 10], ["plain", "guided"], oracle="synthetic",
                  seed=0, out=out, measure=measure, culprits=CULPRITS, emit=lambda _: None)
    assert len(records) == 8 and all(r.found_culprit for r in records)
    assert calls[0] == ("opspilot", "plain", 5, "synthetic")
    assert list(read(out, ScalingRecord)) == records
