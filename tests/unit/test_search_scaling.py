"""Scaling harness: 5/10/20-change releases on the synthetic oracle (free)."""

from __future__ import annotations

import pytest

from sandbox.flags import HARMLESS, baseline_config, config
from search.scaling import build_release, main, measure
from tests.unit.sandbox_helpers import adapter


def test_build_release_sizes():
    for n in (5, 10, 20):
        captured, suspects = build_release(n)
        assert len(suspects) == n and [s.flag for s in suspects] == sorted(s.flag for s in suspects)
        assert all(captured[s.flag] == s.new for s in suspects)
    with pytest.raises(ValueError):
        build_release(21)


@pytest.mark.parametrize(("domain", "culprit"), [
    ("opspilot", ["agent_prompt_version"]),
    ("vaultguard", ["agent_prompt_version", "agent_tool_policy"]),
])
def test_search_grows_slowly(domain, culprit):
    rows = [measure(domain, n) for n in (5, 10, 20)]
    assert all(r["outcome"] == "found" and r["culprit"] == culprit for r in rows)
    configs = [r["search_configs"] for r in rows]
    assert configs == sorted(configs) and configs[-1] - configs[0] <= 4  # ~log N, not 2^N
    assert rows[-1]["exhaustive_runs"] == 3 * 2 ** 20


def test_cli_prints_table(capsys):
    assert main(["--sizes", "5,10"]) == 0
    assert "| 10 | agent_prompt_version |" in capsys.readouterr().out


def test_agents_read_harmless_flags(tmp_path):
    ops = adapter(tmp_path=tmp_path)
    flag = next(iter(HARMLESS))
    cfg = config({**baseline_config().values, flag: HARMLESS[flag][1]})
    ops.run_agent("hero", cfg, ops.identity("hero"), ops.reset_state("hero"))
    assert f"{flag}={HARMLESS[flag][1]}" in ops.provider.opened[-1]["system"]
    ops.run_agent("hero", baseline_config(), ops.identity("hero"), ops.reset_state("hero"))
    assert "Formatting settings" not in ops.provider.opened[-1]["system"]


def test_guided_mode_uses_the_investigators_ranking():
    from agent.llm import FakeLLM

    env = {"MODEL_CHEAP": "m", "MODEL_FLAGSHIP": "f"}
    culprit_first = FakeLLM(['["agent_prompt_version", "agent_model"]'])
    guided = measure("opspilot", 10, mode="guided", llm=culprit_first, env=env)
    plain = measure("opspilot", 10)
    assert guided["order_source"] == "llm" and plain["order_source"] == "diff"
    assert guided["culprit"] == plain["culprit"] == ["agent_prompt_version"]
    assert guided["search_configs"] <= plain["search_configs"]
    broken = measure("opspilot", 5, mode="guided", llm=FakeLLM(["no idea"]), env=env)
    assert broken["order_source"] == "diff"  # falls back, and the table says so
