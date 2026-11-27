"""capture_bench: a bench release becomes bench/incidents/<case>.json that run_arms can load."""

from __future__ import annotations

import json

import pytest

from bench.run_arms import load_incident
from orchestrator.snapshots import load_snapshot, release_diff
from sandbox.capture_bench import CaptureError, capture_case, release_values
from tests.unit.sandbox_helpers import adapter

RELEASE = {"case": "opspilot-dev-1", "domain": "opspilot", "set": "dev", "seed": 1,
           "changes": [{"flag": "agent_model", "old": "model_flagship", "new": "model_cheap"},
                       {"flag": "agent_prompt_version", "old": "v1_strict", "new": "v2_fast"},
                       {"flag": "agent_max_steps", "old": 6, "new": 12}]}


@pytest.fixture
def dirs(tmp_path):
    releases, incidents = tmp_path / "releases", tmp_path / "incidents"
    releases.mkdir()
    (releases / "opspilot-dev-1.json").write_text(json.dumps(RELEASE))
    return releases, incidents


def test_release_values():
    base, rel = release_values(RELEASE)
    assert base["agent_prompt_version"] == "v1_strict" and rel["agent_prompt_version"] == "v2_fast"
    assert rel["agent_tool_policy"] == base["agent_tool_policy"]  # untouched flags stay baseline


def test_capture_writes_case_named_incident(dirs, tmp_path):
    releases, incidents = dirs
    ops = adapter(tmp_path=tmp_path / "w")
    incident = capture_case("opspilot-dev-1", releases_dir=releases, out_dir=incidents,
                            adapter=ops)
    assert incident.id == "opspilot-dev-1"
    loaded = load_incident("opspilot-dev-1", incidents)  # what run_arms does
    assert loaded.resolved_flags.values["agent_prompt_version"] == "v2_fast"
    diff = release_diff(load_snapshot(loaded.baseline_snapshot, None),
                        load_snapshot(loaded.release_snapshot, None))
    assert sorted(c.flag for c in diff) == ["agent_max_steps", "agent_model",
                                            "agent_prompt_version"]


def test_capture_refuses_a_release_that_breaks_nothing(dirs, tmp_path):
    releases, incidents = dirs
    safe = {**RELEASE, "case": "opspilot-dev-2",
            "changes": [{"flag": "agent_max_steps", "old": 6, "new": 12}]}
    (releases / "opspilot-dev-2.json").write_text(json.dumps(safe))
    with pytest.raises(CaptureError, match="no violating run"):
        capture_case("opspilot-dev-2", releases_dir=releases, out_dir=incidents,
                     adapter=adapter(tmp_path=tmp_path / "w"), attempts=2)
    with pytest.raises(CaptureError, match="no release"):
        capture_case("missing", releases_dir=releases, out_dir=incidents)
