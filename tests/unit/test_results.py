import json
from datetime import UTC, datetime, timedelta

import pytest

from bench import results
from bench.records import MANUAL_LOG_COLUMNS, Arm, RunRecord, ScalingRecord, write_all
from bench.results import (
    ManualLogError,
    fmt_duration,
    load_keys,
    load_manual_log,
    render,
    summarize,
)

T0 = datetime(2026, 11, 28, 14, 0, 0, tzinfo=UTC)
HEADER = ",".join(MANUAL_LOG_COLUMNS)
WHEN = datetime(2026, 11, 28, 18, 30, tzinfo=UTC)


def _csv(tmp_path, *rows):
    path = tmp_path / "manual_log.csv"
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _run(case, arm, *, applied_s=None, first_s=None, culprit=(), reverted=(), unsolved=False,
         **extra):
    return RunRecord(
        case=case,
        domain=case.split("-")[0],
        arm=arm,
        started_at=T0,
        first_suspect_at=T0 + timedelta(seconds=first_s) if first_s is not None else None,
        applied_at=T0 + timedelta(seconds=applied_s) if applied_s is not None else None,
        unsolved=unsolved,
        reported_culprit=list(culprit),
        flags_reverted=list(reverted),
        **extra,
    )


def _key(*flags):
    return {"domain": "opspilot", "seed": 1, "bad_id": "x", "set": "eval", "salt": "00",
            "bad_changes": [{"flag": f, "new": "v"} for f in flags], "safe_ids": []}


def _rows(markdown):
    """Results-table rows as label -> [Manual, Global rollback, Plain ddmin, TrustGate]."""
    out = {}
    for line in markdown.split("## What this shows")[0].splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] not in ("Metric", "Release size", "---"):
            out[cells[0]] = cells[1:]
    return out


# ---------------------------------------------------------------- manual log


def test_manual_csv_converts_rows(tmp_path):
    path = _csv(
        tmp_path,
        "opspilot-eval-3,khyati,2026-11-28T14:02:10+05:30,2026-11-28T14:06:45+05:30,"
        "2026-11-28T14:21:30+05:30,7,agent_prompt_version;agent_tool_policy,"
        "prompt reverted globally,agent_prompt_version; agent_tool_policy ;,no,felt easy",
        ",,,,,,,,,,",
        "vaultguard-eval-2,naman,2026-11-28T15:00:00+05:30,,2026-11-28T15:30:00+05:30,,,,,yes,",
    )
    solved, unsolved = load_manual_log(path)

    assert solved.arm == Arm.MANUAL
    assert solved.domain == "opspilot"
    assert solved.person == "khyati"
    assert solved.applied_at == datetime.fromisoformat("2026-11-28T14:21:30+05:30")
    assert solved.human_active_ms == (19 * 60 + 20) * 1000
    assert solved.diagnostic_runs == 7
    assert solved.reported_culprit == ["agent_prompt_version", "agent_tool_policy"]
    assert solved.flags_reverted == ["agent_prompt_version", "agent_tool_policy"]
    assert not solved.unsolved
    assert "prompt reverted globally" in solved.notes and "felt easy" in solved.notes

    assert unsolved.domain == "vaultguard"
    assert unsolved.unsolved
    assert unsolved.applied_at is None
    assert unsolved.first_suspect_at is None
    assert unsolved.diagnostic_runs is None
    assert unsolved.reported_culprit == [] and unsolved.flags_reverted == []
    assert unsolved.human_active_ms == 30 * 60 * 1000


def test_header_only_manual_log_is_empty(tmp_path):
    assert load_manual_log(_csv(tmp_path)) == []
    assert load_manual_log(tmp_path / "missing.csv") == []


@pytest.mark.parametrize("bad_row, message", [
    ("opspilot-eval-1,a,not-a-time,,2026-11-28T14:21:30+05:30,1,x,,x,no,", "started_at"),
    ("opspilot-eval-1,a,2026-11-28T14:00:00+05:30,,2026-11-28T14:21:30+05:30,1,x,,x,maybe,",
     "unsolved"),
    ("opspilot-eval-1,a,2026-11-28T14:00:00+05:30", "columns"),
    ("opspilot-eval-1,a,2026-11-28T14:00:00,,2026-11-28T14:21:30,1,x,,x,no,", "timezone"),
    ("opspilot-eval-1,a,2026-11-28T14:00:00+05:30,,,1,x,,x,no,", "done_at"),
    ("opspilot-eval-1,a,2026-11-28T14:00:00+05:30,,2026-11-28T14:21:30+05:30,seven,x,,x,no,",
     "replays_run"),
])
def test_malformed_row_names_the_line(tmp_path, bad_row, message):
    good = "opspilot-eval-2,a,2026-11-28T14:00:00+05:30,,2026-11-28T14:10:00+05:30,1,x,,x,no,"
    path = _csv(tmp_path, good, "", bad_row)
    with pytest.raises(ManualLogError, match=rf"manual_log\.csv:4: .*{message}"):
        load_manual_log(path)


def test_wrong_header_is_rejected(tmp_path):
    path = tmp_path / "manual_log.csv"
    path.write_text("case,person\n", encoding="utf-8")
    with pytest.raises(ManualLogError, match=r":1: header"):
        load_manual_log(path)


# ---------------------------------------------------------------- calculations


def test_durations_format():
    assert fmt_duration(38_000) == "38s"
    assert fmt_duration(252_000) == "4m 12s"
    assert fmt_duration(65_000) == "1m 05s"
    assert fmt_duration(3_725_000) == "1h 02m"
    assert fmt_duration(None) == "—"


def test_medians_and_range_exclude_unsolved():
    runs = [
        _run("opspilot-eval-1", Arm.GUIDED, applied_s=60, human_active_ms=10_000,
             diagnostic_runs=4, inference_usd=0.10, cost_kept=0.9),
        _run("opspilot-eval-2", Arm.GUIDED, applied_s=120, human_active_ms=20_000,
             diagnostic_runs=6, inference_usd=0.25, cost_kept=0.7),
        _run("opspilot-eval-3", Arm.GUIDED, applied_s=300, human_active_ms=40_000,
             diagnostic_runs=9, inference_usd=0.05, cost_kept=0.8),
        _run("opspilot-eval-4", Arm.GUIDED, unsolved=True, human_active_ms=900_000,
             diagnostic_runs=20),
    ]
    s = summarize(runs, {})["arms"]["guided"]
    assert s["n"] == 4 and s["unsolved"] == 1
    assert s["applied_ms_median"] == 120_000
    assert s["applied_ms_min"] == 60_000 and s["applied_ms_max"] == 300_000
    assert s["human_active_ms_median"] == 20_000
    assert s["diagnostic_runs_median"] == 7.5  # diagnostic runs are not a time: unsolved counts

    rows = _rows(render(summarize(runs, {}), [], WHEN))
    trustgate = 3
    assert rows["Median time to applied fix (range)"][trustgate] == "2m 00s (1m 00s–5m 00s)"
    assert rows["Human active time (median)"][trustgate] == "20s"
    assert rows["Cost saving kept vs release (median)"][trustgate] == "80%"
    assert rows["Diagnostic runs (median)"][trustgate] == "7.5"
    assert rows["Inference cost (total)"][trustgate] == "$0.40"


def test_empty_arms_show_dash():
    runs = [_run("opspilot-eval-1", Arm.PLAIN, applied_s=60, diagnostic_runs=5)]
    md = render(summarize(runs, {}), [], WHEN)
    rows = _rows(md)
    for label, cells in rows.items():
        if label in ("5", "10", "20"):
            continue
        manual, rollback, _, trustgate = cells
        assert manual == rollback == trustgate == "—", label
    assert rows["Inference cost (total)"][2] == "—"  # plain arm has runs but no cost data
    assert "n per arm: Manual 0 · Global rollback 0 · Plain ddmin 1 · TrustGate 0" in md


def test_attribution_and_unnecessary_reverts_with_keys():
    keys = {"opspilot-eval-1": _key("agent_prompt_version"),
            "opspilot-eval-2": _key("model_temperature")}
    runs = [
        _run("opspilot-eval-1", Arm.GLOBAL_ROLLBACK, applied_s=30,
             reverted=["agent_prompt_version", "a", "b", "c"]),
        _run("opspilot-eval-2", Arm.GLOBAL_ROLLBACK, applied_s=30,
             reverted=["model_temperature", "a"]),
        _run("opspilot-eval-1", Arm.GUIDED, applied_s=200, first_s=40,
             culprit=["agent_prompt_version"], reverted=["agent_prompt_version"]),
        _run("opspilot-eval-2", Arm.GUIDED, applied_s=300, first_s=90,
             culprit=["wrong_flag"], reverted=["wrong_flag"]),
    ]
    summary = summarize(runs, keys)
    guided, rollback = summary["arms"]["guided"], summary["arms"]["global_rollback"]
    assert guided["correct"] == 1 and guided["keyed"] == 2
    assert guided["unnecessary_reverts_mean"] == 0.5
    assert guided["first_correct_suspect_ms_median"] == 40_000  # only the correct run counts
    assert rollback["correct"] == 0
    assert rollback["unnecessary_reverts_mean"] == 2.0

    rows = _rows(render(summary, [], WHEN))
    assert rows["Correct attribution"] == ["—", "0 of 2", "—", "1 of 2"]
    assert rows["Flags reverted unnecessarily (mean)"] == ["—", "2", "—", "0.5"]
    assert rows["Time to first suspect, correct runs (median)"][3] == "40s"
    assert rows["Time to first suspect, correct runs (median)"][1] == "—"


def test_interaction_case_counting():
    keys = {"vaultguard-eval-1": _key("agent_tool_policy", "agent_prompt_version"),
            "vaultguard-eval-2": _key("agent_tool_policy", "agent_prompt_version"),
            "opspilot-eval-1": _key("agent_prompt_version")}
    runs = [
        _run("vaultguard-eval-1", Arm.PLAIN, applied_s=10,
             culprit=["agent_prompt_version", "agent_tool_policy"]),
        _run("vaultguard-eval-2", Arm.PLAIN, applied_s=10, culprit=["agent_tool_policy"]),
        _run("opspilot-eval-1", Arm.PLAIN, applied_s=10, culprit=["agent_prompt_version"]),
        _run("opspilot-eval-1", Arm.GUIDED, applied_s=10, culprit=["agent_prompt_version"]),
    ]
    summary = summarize(runs, keys)
    plain = summary["arms"]["plain"]
    assert (plain["interaction_found"], plain["interaction_total"]) == (1, 2)
    assert plain["correct"] == 2
    rows = _rows(render(summary, [], WHEN))
    assert rows["Interaction cases found"][2] == "1 of 2"
    assert rows["Interaction cases found"][3] == "—"  # guided saw no interaction case


def test_attribution_rows_pending_without_keys():
    runs = [_run("opspilot-eval-1", Arm.GUIDED, applied_s=60, culprit=["x"], first_s=5),
            _run("opspilot-eval-1", Arm.MANUAL, applied_s=600, culprit=["x"], first_s=50)]
    md = render(summarize(runs, {}), [], WHEN)
    rows = _rows(md)
    for label in ("Correct attribution", "Flags reverted unnecessarily (mean)",
                  "Interaction cases found", "Time to first suspect, correct runs (median)"):
        assert rows[label] == ["pending", "—", "—", "pending"], label
    assert "pending because the answer keys in bench/keys/ are still sealed" in md
    assert "n = 1 per arm is a small demonstration, not grounds for broad statistical claims." in md
    assert "Generated 2026-11-28 18:30 UTC from bench/records — do not edit by hand" in md


def test_partial_keys_note():
    keys = {"opspilot-eval-1": _key("x")}
    runs = [_run("opspilot-eval-1", Arm.GUIDED, applied_s=60, culprit=["x"]),
            _run("opspilot-eval-2", Arm.GUIDED, applied_s=60, culprit=["x"])]
    md = render(summarize(runs, keys), [], WHEN)
    assert _rows(md)["Correct attribution"][3] == "1 of 1"
    assert "Attribution rows cover 1 of 2 runs" in md


def test_scaling_table():
    scaling = [
        ScalingRecord(case="c1", release_size=5, arm=Arm.PLAIN, search_runs=8, found_culprit=True),
        ScalingRecord(case="c2", release_size=5, arm=Arm.PLAIN, search_runs=12,
                      found_culprit=True),
        ScalingRecord(case="c1", release_size=5, arm=Arm.GUIDED, search_runs=3,
                      found_culprit=True),
        ScalingRecord(case="c3", release_size=20, arm=Arm.GUIDED, search_runs=7,
                      found_culprit=False),
    ]
    rows = _rows(render(summarize([], {}), scaling, WHEN))
    assert rows["5"] == ["10 (n=2)", "3 (n=1)"]
    assert rows["10"] == ["—", "—"]
    assert rows["20"] == ["—", "7 (n=1)"]


# ---------------------------------------------------------------- loading and CLI


def test_load_keys(tmp_path):
    assert load_keys(tmp_path / "absent") == {}
    (tmp_path / "opspilot-eval-1.json").write_text(json.dumps(_key("a")), encoding="utf-8")
    (tmp_path / "hashes.txt").write_text("opspilot-eval-1 abc\n", encoding="utf-8")
    keys = load_keys(tmp_path)
    assert list(keys) == ["opspilot-eval-1"]


def test_cli_writes_output(tmp_path, capsys):
    runs_path = tmp_path / "runs.jsonl"
    write_all(runs_path, [_run("opspilot-eval-1", Arm.GUIDED, applied_s=90,
                               culprit=["agent_prompt_version"], inference_usd=0.12)])
    scaling_path = tmp_path / "scaling.jsonl"
    write_all(scaling_path, [ScalingRecord(case="c", release_size=10, arm=Arm.PLAIN,
                                           search_runs=6, found_culprit=True)])
    manual = _csv(tmp_path, "opspilot-eval-1,arnab,2026-11-28T14:00:00+00:00,,"
                            "2026-11-28T14:04:12+00:00,5,agent_prompt_version,,"
                            "agent_prompt_version,no,")
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "opspilot-eval-1.json").write_text(json.dumps(_key("agent_prompt_version")),
                                               encoding="utf-8")
    out = tmp_path / "out" / "results.md"

    results.main(["--runs", str(runs_path), "--manual", str(manual), "--scaling",
                  str(scaling_path), "--keys", str(keys), "--out", str(out)])

    md = out.read_text(encoding="utf-8")
    rows = _rows(md)
    assert rows["Human active time (median)"][0] == "4m 12s"
    assert rows["Correct attribution"] == ["1 of 1", "—", "—", "1 of 1"]
    assert rows["10"] == ["6 (n=1)", "—"]
    assert "wrote" in capsys.readouterr().out


def test_cli_reports_malformed_manual_log(tmp_path):
    manual = _csv(tmp_path, "opspilot-eval-1,a,yesterday,,,,,,,no,")
    with pytest.raises(SystemExit, match=r"manual_log\.csv:2"):
        results.main(["--runs", str(tmp_path / "none.jsonl"), "--manual", str(manual),
                      "--keys", str(tmp_path), "--out", str(tmp_path / "r.md")])


def test_zero_active_time_on_an_approved_automated_run_counts_as_missing():
    runs = [
        _run("opspilot-eval-1", Arm.PLAIN, applied_s=600, human_active_ms=0),
        _run("opspilot-eval-2", Arm.PLAIN, applied_s=600, human_active_ms=8000),
        _run("opspilot-eval-3", Arm.PLAIN, applied_s=600, human_active_ms=4000),
    ]
    plain = summarize(runs, {})["arms"]["plain"]
    assert plain["human_active_ms_median"] == 6000
    assert plain["human_active_missing"] == 1
    md = render(summarize(runs, {}), [], generated_at=WHEN)
    assert "not measured on 1 Plain ddmin run:" in md


def test_excluded_records_leave_the_arm_totals_and_are_listed():
    runs = [
        _run("opspilot-eval-1", Arm.GUIDED, applied_s=600, investigation_id="inv_old"),
        _run("opspilot-eval-1", Arm.GUIDED, unsolved=True, investigation_id="inv_expired"),
        _run("opspilot-eval-1", Arm.GUIDED, applied_s=600, investigation_id="inv_new"),
    ]
    excluded = {"inv_old": "Superseded.", "inv_expired": "Expired."}
    summary = summarize(runs, {}, excluded)
    assert summary["arms"]["guided"]["n"] == 1
    assert summary["arms"]["guided"]["unsolved"] == 0
    md = render(summary, [], generated_at=WHEN)
    assert "not in the TrustGate totals: opspilot-eval-1 (inv_old). Superseded." in md
    assert "(inv_expired). Expired." in md


def test_load_excluded(tmp_path):
    assert results.load_excluded(tmp_path / "missing.json") == {}
    path = tmp_path / "excluded.json"
    path.write_text(json.dumps([{"investigation_id": "inv_1", "reason": "r"}]), encoding="utf-8")
    assert results.load_excluded(path) == {"inv_1": "r"}


def test_expectations_pending_until_their_arms_exist():
    runs = [_run("opspilot-eval-1", Arm.GUIDED, applied_s=600, human_active_ms=5000,
                 cost_kept=1.0, validated_repair=True)]
    verdicts = [v for _, v, _ in results.expectations(summarize(runs, {}), {})]
    assert verdicts == ["pending"] * 4


def test_expectations_held_and_failed_from_the_numbers():
    runs = [
        _run("vaultguard-eval-1", Arm.GUIDED, applied_s=600, human_active_ms=5000, cost_kept=0.9,
             validated_repair=True, culprit=["a", "b"]),
        _run("vaultguard-eval-1", Arm.MANUAL, applied_s=900, human_active_ms=900_000,
             culprit=["a"]),
        _run("vaultguard-eval-1", Arm.GLOBAL_ROLLBACK, cost_kept=0.0,
             notes="held-out 18/18 passed (sandbox)"),
    ]
    keys = {"vaultguard-eval-1": {"bad_changes": [{"flag": "a"}, {"flag": "b"}]}}
    scale = {10: {"guided": {"median": 12, "n": 1}, "plain": {"median": 11, "n": 1}},
             20: {"guided": {"median": 12, "n": 1}, "plain": {"median": 13, "n": 1}}}
    got = results.expectations(summarize(runs, keys), scale)
    assert [v for _, v, _ in got] == ["held", "held", "held", "failed"]
    assert "global rollback held-out 18/18" in got[1][2]
    assert "10 flags: 12 against 11" in got[3][2]


def test_render_has_the_shows_and_doesnt_show_blocks():
    runs = [_run("opspilot-eval-1", Arm.PLAIN, applied_s=600, first_s=120, diagnostic_runs=5,
                 cost_kept=1.0)]
    md = render(summarize(runs, {}), [], generated_at=WHEN)
    assert "## What this shows" in md and "## What this doesn't show" in md
    assert "| opspilot-eval-1 | Plain ddmin | 5 | 2m 00s | 100% | — |" in md
