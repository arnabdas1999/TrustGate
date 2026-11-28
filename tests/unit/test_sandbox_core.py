"""Sandbox core: flag resolution, the LLM loop and Gemini wire format, runner, cache, live, CLI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from contracts.models import Identity
from sandbox import cache, flags, live, llm, replay
from sandbox.runner import run_batch_sync
from tests.unit.sandbox_helpers import ENV, adapter, bad_hero, by_prompt, good_hero

TIER0 = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
STAGING = Identity(identifier="staging-api", traits={"env": "staging", "tier": 2})
PROD1 = Identity(identifier="reports-prod", traits={"env": "prod", "tier": 1})


# -- flags --------------------------------------------------------------------------------------


def test_release_changes_five_flags():
    changed = {k for k in flags.BASELINE if flags.BASELINE[k] != flags.RELEASE[k]}
    assert changed == set(flags.RELEASE_CHANGES) and len(changed) == 5


def test_segment_override_matches_tier0_only():
    cfg = flags.config(flags.RELEASE, segment_overrides={
        "tier0_production": {"agent_prompt_version": "v1_strict"}})
    assert flags.effective_values(cfg, TIER0)["agent_prompt_version"] == "v1_strict"
    assert flags.effective_values(cfg, STAGING)["agent_prompt_version"] == "v2_fast"
    assert flags.effective_values(cfg, PROD1)["agent_prompt_version"] == "v2_fast"


def test_high_value_wires_segment():
    cfg = flags.config(flags.RELEASE, segment_overrides={
        "high_value_wires": {"agent_tool_policy": "restricted"}})
    big = Identity(identifier="w1", traits={"amount_usd": 25000, "new_vendor": False})
    new = Identity(identifier="w2", traits={"amount_usd": 500, "new_vendor": True})
    small = Identity(identifier="w3", traits={"amount_usd": 500, "new_vendor": False})
    assert flags.effective_values(cfg, big)["agent_tool_policy"] == "restricted"
    assert flags.effective_values(cfg, new)["agent_tool_policy"] == "restricted"
    assert flags.effective_values(cfg, small)["agent_tool_policy"] == "expanded"


def test_unknown_segment_is_an_error():
    with pytest.raises(KeyError):
        flags.effective_values(flags.config(flags.RELEASE, segment_overrides={"x": {}}), TIER0)


def test_safe_fallback_serves_baseline():
    cfg = flags.config({**flags.RELEASE, "agent_safe_fallback": True})
    values = flags.effective_values(cfg, STAGING)
    assert values["agent_prompt_version"] == "v1_strict" and values["agent_safe_fallback"]


def test_parse_value():
    assert flags.parse_value("12") == 12 and flags.parse_value("true") is True
    assert flags.parse_value("0.0") == "0.0" and flags.parse_value("v2_fast") == "v2_fast"


# -- loop and prices ------------------------------------------------------------------------------


def test_model_ids_and_prices():
    assert llm.model_id("model_cheap", ENV) == "cheap-model-x"
    with pytest.raises(llm.LLMError):
        llm.model_id("model_cheap", {})
    with pytest.raises(llm.LLMError):
        llm.model_id("gpt", ENV)
    assert llm.cost_usd("model_flagship", 1_000_000, 0, {}) == 1.25
    assert llm.cost_usd("model_cheap", 0, 1_000_000, {"PRICE_MODEL_CHEAP": "1,4"}) == 4.0


def test_loop_tool_errors_go_back_to_model():
    chat = llm.ScriptedChat([llm.Turn(calls=[llm.Call("boom", {})]), llm.Turn(text="ok")])

    def dispatch(name, args):
        raise RuntimeError("kaput")

    result = llm.run_tool_loop(chat, dispatch, max_steps=5)
    assert result.tool_calls[0].result == {"ok": False, "error": "RuntimeError: kaput"}
    assert result.final_text == "ok" and not result.hit_step_limit
    assert chat.received[1][0][1]["ok"] is False


def test_loop_timeout():
    ticks = iter([0.0, 0.0, 500.0])
    chat = llm.ScriptedChat([llm.Turn(calls=[llm.Call("a", {})])] * 5)
    result = llm.run_tool_loop(chat, lambda n, a: {}, max_steps=5, timeout_s=10,
                               clock=lambda: next(ticks))
    assert result.error and "timeout" in result.error


# -- Gemini wire format ---------------------------------------------------------------------------


def _gemini(responses):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == "k"
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    provider = llm.GeminiProvider("k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                                  sleep=lambda s: None)
    return provider, sent


def test_gemini_function_calling_round_trip():
    call_part = {"functionCall": {"name": "get_cluster_status", "args": {"cluster": "a"}},
                 "thoughtSignature": "sig123"}
    provider, sent = _gemini([
        (200, {"candidates": [{"content": {"role": "model", "parts": [call_part]}}],
               "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 5,
                                 "thoughtsTokenCount": 10}}),
        (200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}],
               "usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 3}}),
    ])
    spec = llm.ToolSpec("get_cluster_status", "d", {"type": "object", "properties": {
        "cluster": {"type": "string"}}, "required": ["cluster"]})
    chat = provider.open_chat(model="m", system="sys", user="hi", tools=[spec], temperature=0.0,
                              seed=3)
    result = llm.run_tool_loop(chat, lambda n, a: {"ok": True, "health": "healthy"}, max_steps=4)
    assert [c.name for c in result.tool_calls] == ["get_cluster_status"]
    assert result.tokens_in == 120 and result.tokens_out == 18
    first, second = sent
    decl = first["tools"][0]["functionDeclarations"][0]
    assert decl["parameters"]["type"] == "OBJECT"
    assert decl["parameters"]["properties"]["cluster"]["type"] == "STRING"
    assert first["generationConfig"] == {"temperature": 0.0, "seed": 3}
    assert second["contents"][1]["parts"][0]["thoughtSignature"] == "sig123"
    assert second["contents"][2]["parts"][0]["functionResponse"] == {
        "name": "get_cluster_status", "response": {"ok": True, "health": "healthy"}}


def test_gemini_retries_429_then_succeeds():
    ok = {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
    provider, sent = _gemini([(429, {"error": {"message": "slow down"}}), (200, ok)])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    assert chat.send(None).text == "x" and len(sent) == 2


def test_gemini_client_error_raises_without_key():
    provider, _ = _gemini([(400, {"error": {"message": "bad request"}})])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    with pytest.raises(llm.LLMError) as err:
        chat.send(None)
    assert "400" in str(err.value) and "k" not in repr(provider)


def test_gemini_blocked_prompt():
    provider, _ = _gemini([(200, {"promptFeedback": {"blockReason": "SAFETY"}})])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    with pytest.raises(llm.LLMError, match="SAFETY"):
        chat.send(None)


def test_provider_from_env():
    assert isinstance(llm.provider_from_env({"LLM_API_KEY": "k"}), llm.GeminiProvider)
    with pytest.raises(llm.LLMError):
        llm.provider_from_env({"LLM_PROVIDER": "other", "LLM_API_KEY": "k"})
    with pytest.raises(llm.LLMError):
        llm.provider_from_env({})


# -- runner ---------------------------------------------------------------------------------------


def test_batch_counts_failures(tmp_path):
    ops = adapter(tmp_path=tmp_path)
    batch = run_batch_sync(ops, "hero", flags.release_config(), 5, parallel=3)
    assert len(batch.runs) == 5 and batch.failures == 5 and batch.errors == 0
    assert [r.seed for r in batch.runs] == [0, 1, 2, 3, 4]
    base = run_batch_sync(ops, "hero", flags.baseline_config(), 5)
    assert base.failures == 0 and base.passes == 5
    assert not list(tmp_path.glob("run-*.db"))  # run copies cleaned up


def test_batch_early_stop(tmp_path):
    ops = adapter(tmp_path=tmp_path)
    batch = run_batch_sync(ops, "hero", flags.release_config(), 10, parallel=1, early_stop=True)
    assert len(batch.runs) == 1 and batch.failures == 1


def test_errored_runs_are_neither_pass_nor_fail(tmp_path):
    class Down:
        def open_chat(self, **kw):
            raise llm.LLMError("provider down")

    ops = adapter(tmp_path=tmp_path)
    ops._provider = Down()
    batch = run_batch_sync(ops, "hero", flags.release_config(), 3)
    assert batch.errors == 3 and batch.failures == 0 and batch.passes == 0


# -- cache ----------------------------------------------------------------------------------------


def test_cache_round_trip_is_display_only(tmp_path):
    ops = adapter(tmp_path=tmp_path / "w")
    run = run_batch_sync(ops, "hero", flags.release_config(), 1).runs[0]
    store = cache.DisplayCache(tmp_path / "c")
    key = cache.cache_key(flags.release_config(), "hero", 0, model="m",
                          prompt_hash=cache.prompt_hash("p"))
    assert store.get(key) is None
    store.put(key, run.trace, run.grade)
    got = store.get(key)
    assert got.fresh is False and got.grade.violations == run.grade.violations
    other = cache.cache_key(flags.baseline_config(), "hero", 0, model="m",
                            prompt_hash=cache.prompt_hash("p"))
    assert other != key


# -- live -----------------------------------------------------------------------------------------


class FakeFlags:
    def __init__(self, values, defaulted=()):
        self.values, self.defaulted = values, set(defaulted)

    def get_flag(self, name):
        return SimpleNamespace(value=self.values.get(name), is_default=name in self.defaulted)


class FakeClient:
    def __init__(self, flags_):
        self.flags, self.calls = flags_, []

    def get_identity_flags(self, identifier, traits):
        self.calls.append((identifier, traits))
        return self.flags


def test_live_resolve_marks_defaults():
    client = FakeClient(FakeFlags(flags.RELEASE, defaulted=["agent_model"]))
    cfg, defaulted = live.resolve(client, TIER0)
    assert cfg.values == flags.RELEASE and cfg.source == "captured"
    assert defaulted == ["agent_model"]
    assert client.calls == [("payments-prod", {"env": "prod", "tier": 0})]


def test_live_run_captures_incident_on_violation(tmp_path):
    ops = adapter(tmp_path=tmp_path / "w")
    client = FakeClient(FakeFlags(flags.RELEASE))
    incident, trace, _ = live.run_live(ops, "hero", client, baseline_snapshot="b.json",
                                       release_snapshot="r.json", out_dir=tmp_path / "inc")
    assert incident is not None and incident.resolved_flags.values == flags.RELEASE
    saved = json.loads((tmp_path / "inc" / f"{incident.id}.json").read_text())
    assert saved["case_id"] == "hero" and saved["trace"]["reported_status"] == "RESOLVED"
    clean = FakeClient(FakeFlags(flags.BASELINE))
    assert live.run_live(ops, "hero", clean, baseline_snapshot="b", release_snapshot="r",
                         out_dir=tmp_path / "inc")[0] is None


def test_live_needs_server_key():
    with pytest.raises(live.LiveError):
        live.make_client("not-a-server-key")


# -- replay CLI -----------------------------------------------------------------------------------


def test_replay_reproduce_gate(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(replay, "get_adapter", lambda d: adapter(by_prompt, tmp_path))
    assert replay.main(["--reproduce"]) == 0
    assert "REPRODUCTION GATE: PASS" in capsys.readouterr().out


def test_replay_gate_fails_when_baseline_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(replay, "get_adapter", lambda d: adapter(lambda **k: bad_hero(), tmp_path))
    assert replay.main(["--reproduce"]) == 1
    assert "REPRODUCTION GATE: FAIL" in capsys.readouterr().out


def test_replay_matrix_and_overrides(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(replay, "get_adapter", lambda d: adapter(by_prompt, tmp_path))
    assert replay.main(["--matrix", "--runs", "1", "--json"]) == 0
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("\n[\n"):])
    fails = {s["label"]: s["failures"] for s in summary}
    assert fails == {"prompt=v1_strict tools=restricted": 0, "prompt=v1_strict tools=expanded": 0,
                     "prompt=v2_fast tools=restricted": 3, "prompt=v2_fast tools=expanded": 3}
    cfg = replay.build_config("release", ["agent_max_steps=6"],
                              ["tier0_production:agent_prompt_version=v1_strict"])
    assert cfg.values["agent_max_steps"] == 6
    assert cfg.segment_overrides == {"tier0_production": {"agent_prompt_version": "v1_strict"}}


def test_good_hero_helper_is_used():
    assert good_hero()[-2].calls[0].name == "report_status"


def test_post_write_check_runs_on_served_flags(tmp_path):
    ops = adapter(tmp_path=tmp_path / "w")
    repaired = {**flags.RELEASE, "agent_prompt_version": "v1_strict"}
    client = FakeClient(FakeFlags(repaired))
    check = live.post_write_check(ops, "hero", client, runs=3,
                                  expect={"agent_prompt_version": "v1_strict"})
    assert check.ok and (check.passes, check.runs) == (3, 3) and check.violations == []
    assert client.calls == [("payments-prod", {"env": "prod", "tier": 0})]
    bad = live.post_write_check(ops, "hero", FakeClient(FakeFlags(flags.RELEASE)), runs=2)
    assert not bad.ok and bad.passes == 0 and "restart_without_change_window" in bad.violations


def test_post_write_check_refuses_defaults_and_wrong_values(tmp_path):
    ops = adapter(tmp_path=tmp_path / "w")
    defaulted = live.post_write_check(
        ops, "hero", FakeClient(FakeFlags(flags.BASELINE, defaulted=["agent_model"])))
    assert not defaulted.ok and defaulted.runs == 0 and defaulted.defaulted == ["agent_model"]
    wrong = live.post_write_check(ops, "hero", FakeClient(FakeFlags(flags.RELEASE)),
                                  expect={"agent_prompt_version": "v1_strict"})
    assert not wrong.ok and wrong.runs == 0 and wrong.mismatched == ["agent_prompt_version"]


def test_live_cli_post_write(monkeypatch, capsys, tmp_path):
    import sandbox

    monkeypatch.setattr(sandbox, "get_adapter", lambda d: adapter(tmp_path=tmp_path / "w"))
    monkeypatch.setenv("BENCH_KEY", "ser.test")
    repaired = {**flags.RELEASE, "agent_prompt_version": "v1_strict"}
    args = ["--post-write", "2", "--key-var", "BENCH_KEY"]
    assert live.main(args, client_factory=lambda k: FakeClient(FakeFlags(repaired))) == 0
    assert "post-write: 2/2 passed" in capsys.readouterr().out
    assert live.main(args, client_factory=lambda k: FakeClient(FakeFlags(flags.RELEASE))) == 1
    monkeypatch.delenv("BENCH_KEY")
    assert live.main(args, client_factory=lambda k: None) == 2
