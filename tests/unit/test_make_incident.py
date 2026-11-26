import hashlib
import json

import pytest

from bench.make_incident import PoolError, build_release, load_pools, write_incident
from contracts import canonical_json


def _verified_pools():
    pools = load_pools()
    for domain in ("opspilot", "vaultguard"):
        for entry in pools[domain]["bad"] + pools[domain]["safe"]:
            entry["verified"] = True
    return pools


def test_same_seed_same_release():
    pools = _verified_pools()
    assert build_release(pools, "opspilot", 7) == build_release(pools, "opspilot", 7)


def test_release_has_4_or_5_unique_flags_and_contains_the_culprit():
    pools = _verified_pools()
    for seed in range(30):
        changes, key = build_release(pools, "opspilot", seed)
        flags = [c["flag"] for c in changes]
        assert len(flags) == len(set(flags))
        assert 4 <= len(flags) <= 5 or len(flags) == 1 + len(pools["opspilot"]["safe"])
        culprit = {(c["flag"], c["new"]) for c in key["bad_changes"]}
        assert culprit <= {(c["flag"], c["new"]) for c in changes}


def test_vaultguard_pair_stays_together():
    changes, key = build_release(_verified_pools(), "vaultguard", 3)
    assert key["bad_id"] == "split_payment_pair"
    applied = {c["flag"]: c["new"] for c in changes}
    assert applied["agent_tool_policy"] == "expanded"
    assert applied["agent_prompt_version"] == "v2_fast"


def test_old_values_come_from_the_baseline():
    pools = _verified_pools()
    changes, _ = build_release(pools, "opspilot", 1)
    for change in changes:
        assert change["old"] == pools["opspilot"]["baseline"][change["flag"]]


def test_unverified_pools_are_refused_by_default():
    with pytest.raises(PoolError):
        build_release(load_pools(), "opspilot", 1)
    changes, _ = build_release(load_pools(), "opspilot", 1, allow_unverified=True)
    assert changes


def test_written_release_hides_the_culprit_and_the_hash_matches(tmp_path):
    release_path, digest = write_incident("opspilot", 11, "dev", bench_dir=tmp_path,
                                          pools=_verified_pools())
    release = json.loads(release_path.read_text())
    text = release_path.read_text()
    assert "bad_id" not in text and "salt" not in text
    assert release["key_sha256"] == digest

    key_path = tmp_path / "keys" / "opspilot-dev-11.json"
    key = json.loads(key_path.read_text())
    assert hashlib.sha256(canonical_json(key).encode()).hexdigest() == digest
    assert len(key["salt"]) == 32
    assert f"opspilot-dev-11 {digest}" in (tmp_path / "keys" / "hashes-dev.txt").read_text()
    assert not (tmp_path / "keys" / "hashes.txt").exists()  # dev runs never touch the proof file


def test_eval_hashes_go_to_the_committed_file_with_lf_endings(tmp_path):
    _, digest = write_incident("vaultguard", 2, "eval", bench_dir=tmp_path,
                               pools=_verified_pools())
    raw = (tmp_path / "keys" / "hashes.txt").read_bytes()
    assert raw == f"vaultguard-eval-2 {digest}\n".encode()
    assert b"\r\n" not in (tmp_path / "releases" / "vaultguard-eval-2.json").read_bytes()


def test_existing_key_is_never_overwritten(tmp_path):
    write_incident("opspilot", 5, "dev", bench_dir=tmp_path, pools=_verified_pools())
    with pytest.raises(FileExistsError):
        write_incident("opspilot", 5, "dev", bench_dir=tmp_path, pools=_verified_pools())
