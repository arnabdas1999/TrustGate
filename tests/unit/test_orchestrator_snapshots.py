import json

import pytest

from orchestrator.snapshots import (
    SnapshotError,
    load_snapshot,
    release_diff,
    segments_document,
)

BASELINE = {"agent_model": "model_flagship", "agent_prompt_version": "v1_strict",
            "agent_max_steps": 6, "agent_temperature": "0.0", "agent_safe_fallback": False}
GATE_VIEW = {"environment": "Development", "fingerprint": "f" * 64, "view": {
    "features": {
        "agent_model": {"enabled": True, "value": "model_cheap", "multivariate": []},
        "agent_prompt_version": {"enabled": True, "value": "v2_fast", "multivariate": []},
        "agent_max_steps": {"enabled": True, "value": "12", "multivariate": []},
        "agent_temperature": {"enabled": True, "value": "0.0", "multivariate": []},
        "agent_safe_fallback": {"enabled": True, "value": False, "multivariate": []}},
    "segments": {"tier0_production": {"rules": [{"type": "ALL", "conditions": [], "rules": []}],
                                      "overrides": {}}}}}


class FakeGate:
    def __init__(self):
        self.reads = []

    def read(self, tool, args):
        self.reads.append((tool, args))
        return GATE_VIEW


def test_file_snapshot_uses_typed_values(tmp_path):
    path = tmp_path / "baseline_env.json"
    path.write_text(json.dumps({"environment": "Development", "values": BASELINE}))
    assert load_snapshot(f"file:{path}", FakeGate()) == BASELINE


def test_gate_snapshot_reads_through_the_gate():
    gate = FakeGate()
    values = load_snapshot("gate:Development", gate)
    assert gate.reads == [("get_environment_document", {"environment": "Development"})]
    assert values["agent_prompt_version"] == "v2_fast"


@pytest.mark.parametrize("ref", ["http://x", "file:", "gate:", "Development"])
def test_bad_refs_are_refused(ref):
    with pytest.raises(SnapshotError):
        load_snapshot(ref, FakeGate())


def test_release_diff_lists_changed_flags_in_name_order():
    release = load_snapshot("gate:Development", FakeGate())
    diff = release_diff(BASELINE, release)
    assert [(c.flag, c.old, c.new) for c in diff] == [
        ("agent_max_steps", 6, "12"),
        ("agent_model", "model_flagship", "model_cheap"),
        ("agent_prompt_version", "v1_strict", "v2_fast")]


def test_string_and_int_of_the_same_number_are_not_a_change():
    assert release_diff({"agent_max_steps": 6}, {"agent_max_steps": "6"}) == []


def test_added_and_removed_flags_are_changes():
    diff = release_diff({"a": 1}, {"b": 2})
    assert [(c.flag, c.old, c.new) for c in diff] == [("a", 1, None), ("b", None, 2)]


def test_segments_document_for_triage():
    doc = segments_document(GATE_VIEW["view"])
    assert doc == {"project": {"segments": [
        {"name": "tier0_production", "rules": [{"type": "ALL", "conditions": [], "rules": []}]}]}}
