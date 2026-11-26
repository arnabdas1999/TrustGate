import json
from datetime import UTC, datetime, timedelta

import pytest

from contracts.models import AuditEntry
from gate.audit_log import AuditLog, AuditTampered, merge_versions

T0 = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl", clock=Clock())


def _write(log, **extra):
    return log.append(actor="executor", action="write",
                      tool="create_environment_feature_version",
                      args={"environment_pk": 121197}, result="applied",
                      proposal_id="prop_1", approval_id="appr_1", approver="naman",
                      payload_hash="h" * 64, fingerprint_before="a" * 64,
                      fingerprint_after="b" * 64, version_uuids=["v-1"], **extra)


def test_append_and_read_back(log):
    _write(log)
    log.append(actor="investigator", action="read", tool="list_project_features",
               args={"project_pk": 56664}, result="ok")
    entries = log.entries()
    assert [e["action"] for e in entries] == ["write", "read"]
    assert entries[0]["approver"] == "naman"
    assert entries[0]["ts"] == "2026-11-26T18:00:01Z"


def test_every_line_is_a_valid_api_audit_entry(log):
    _write(log)
    log.append(actor="investigator", action="refused", tool="list_organizations", args={},
               result="not on the read allowlist")
    for entry in log.entries():
        AuditEntry.model_validate(entry)


def test_unknown_action_is_rejected(log):
    with pytest.raises(ValueError):
        log.append(actor="x", action="delete", tool=None, args={}, result=None)


def test_file_is_append_only_across_instances(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(AuditLog(path, clock=Clock()))
    _write(AuditLog(path, clock=Clock()))
    assert len(path.read_text().splitlines()) == 2
    AuditLog(path).verify_chain()


def test_filter_by_proposal(log):
    _write(log)
    log.append(actor="investigator", action="read", tool="x", args={}, result="ok",
               proposal_id="prop_2")
    assert [e["proposal_id"] for e in log.entries(proposal_id="prop_2")] == ["prop_2"]


def test_chain_detects_an_edited_line(log, tmp_path):
    _write(log)
    _write(log)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["approver"] = "mallory"
    lines[0] = json.dumps(first, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(AuditTampered):
        AuditLog(path).verify_chain()


def test_chain_detects_a_deleted_line(log, tmp_path):
    _write(log)
    _write(log)
    _write(log)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(AuditTampered):
        AuditLog(path).verify_chain()


def test_secrets_in_args_are_masked(log):
    log.append(actor="investigator", action="read", tool="x",
               args={"note": "ser.abcdefghijklmnop", "h": "Api-Key pre.fix123"}, result="ok")
    raw = log.entries()[0]
    assert "ser.abcdefghijklmnop" not in json.dumps(raw)
    assert "pre.fix123" not in json.dumps(raw)


def test_merge_versions_adds_flagsmith_when(log):
    _write(log)
    versions = [
        {"uuid": "v-1", "live_from": "2026-11-26T18:00:02Z", "created_by": None,
         "description": "trustgate proposal=prop_1 approval=appr_1"},
        {"uuid": "v-0", "live_from": "2026-11-26T17:58:14Z", "created_by": 73517,
         "description": None},
    ]
    merged = merge_versions(log.entries(), versions)
    write = merged["gate"][0]
    assert write["flagsmith_live_from"] == ["2026-11-26T18:00:02Z"]
    assert merged["outside_trustgate"] == [
        {"uuid": "v-0", "live_from": "2026-11-26T17:58:14Z", "created_by": 73517,
         "description": None}]


def test_half_written_last_line_is_skipped(log, tmp_path):
    _write(log)
    with (tmp_path / "audit.jsonl").open("a") as fh:
        fh.write('{"ts": "2026-11-26T18:00:09Z", "actor": "exec')  # crash mid-write
    assert len(log.entries()) == 1
    fresh = AuditLog(tmp_path / "audit.jsonl")  # a restarted gate keeps appending safely
    _write(fresh)
    assert len(fresh.entries()) == 2
    fresh.verify_chain()


def test_last_hash_is_cached_not_reread(log, monkeypatch):
    _write(log)
    reads = []
    real_open = type(log.path).open

    def counting_open(self, mode="r", *a, **k):
        if "r" in mode:
            reads.append(mode)
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(type(log.path), "open", counting_open)
    _write(log)
    _write(log)
    assert reads == []
    log.verify_chain()


def test_merge_versions_trusts_only_the_gates_own_uuids(log):
    _write(log)  # the gate wrote version v-1
    versions = [
        {"uuid": "v-1", "live_from": "t1", "description": "trustgate proposal=prop_1"},
        # Someone else labelled their change "trustgate ...": still outside TrustGate.
        {"uuid": "v-evil", "live_from": "t2", "description": "trustgate proposal=prop_9"},
    ]
    merged = merge_versions(log.entries(), versions)
    assert [v["uuid"] for v in merged["outside_trustgate"]] == ["v-evil"]
