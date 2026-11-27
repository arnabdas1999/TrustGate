"""verify_pools on the scripted OpsPilot agent (v2_fast misbehaves, everything else is fine)."""

from __future__ import annotations

import json

from sandbox.verify_pools import main, verdict, verify_domain, write_back
from tests.unit.sandbox_helpers import adapter

POOL = {
    "baseline": {"agent_model": "model_flagship", "agent_prompt_version": "v1_strict",
                 "agent_tool_policy": "restricted", "agent_max_steps": 6,
                 "agent_context_mode": "summary", "agent_temperature": "0.0",
                 "agent_safe_fallback": False},
    "bad": [{"id": "prompt_v2_fast", "verified": False,
             "changes": [{"flag": "agent_prompt_version", "new": "v2_fast"}]},
            {"id": "max_steps_20", "verified": False,
             "changes": [{"flag": "agent_max_steps", "new": 20}]}],
    "safe": [{"id": "model_cheap", "verified": False,
              "changes": [{"flag": "agent_model", "new": "model_cheap"}]},
             {"id": "prompt_v2_fast_as_safe", "verified": False,
              "changes": [{"flag": "agent_prompt_version", "new": "v2_fast"}]}],
}


def test_verdict_rules():
    assert verdict("bad", 8, 0, 10, 0) and not verdict("bad", 7, 0, 10, 0)
    assert not verdict("bad", 10, 0, 10, 1)  # baseline must be clean
    assert verdict("safe", 0, 0, 10, 0) and not verdict("safe", 1, 0, 10, 0)
    assert not verdict("safe", 0, 2, 8, 0)  # errors leave it unverified


def test_verify_and_write_back(tmp_path):
    rows = verify_domain("opspilot", POOL, runs=3, adapter=adapter(tmp_path=tmp_path))
    got = {r["id"]: r["verified"] for r in rows}
    assert got == {"baseline": True, "prompt_v2_fast": True, "max_steps_20": False,
                   "model_cheap": True, "prompt_v2_fast_as_safe": False}
    pools = {"opspilot": json.loads(json.dumps(POOL))}
    write_back(pools, rows, {"MODEL_CHEAP": "c", "MODEL_FLAGSHIP": "f"})
    bad = pools["opspilot"]["bad"][0]
    assert bad["verified"] is True and bad["evidence"]["failures"] == 3
    assert bad["evidence"]["models"] == {"cheap": "c", "flagship": "f"}


def test_cli_writes_pools(tmp_path, monkeypatch, capsys):
    path = tmp_path / "pools.json"
    path.write_text(json.dumps({"opspilot": POOL}))
    monkeypatch.setattr("sandbox.verify_pools.get_adapter", lambda d: adapter(tmp_path=tmp_path))
    assert main(["--domain", "opspilot", "--runs", "2", "--only", "prompt_v2_fast",
                 "--write", "--pools", str(path)]) == 0
    assert json.loads(path.read_text())["opspilot"]["bad"][0]["verified"] is True
    assert "| opspilot | bad | prompt_v2_fast | 2/2 |" in capsys.readouterr().out
