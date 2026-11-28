"""scripts.demo_incident with fakes: no Flagsmith, no models, no network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import demo_incident as demo


class FakeIncident:
    def __init__(self, incident_id: str = "opspilot-hero-abc12345") -> None:
        self.id = incident_id

    def model_dump(self, mode: str = "json") -> dict:
        return {"id": self.id, "domain": "opspilot"}


def _trace(status: str = "resolved") -> SimpleNamespace:
    return SimpleNamespace(reported_status=status, tool_calls=[SimpleNamespace(name="restart")])


def _snapshots(tmp_path: Path) -> tuple[Path, Path]:
    base, rel = tmp_path / "b.json", tmp_path / "r.json"
    base.write_text("{}")
    rel.write_text("{}")
    return base, rel


def _api(log: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        log.append((request.method, request.url.path, body))
        if request.url.path == "/api/investigations":
            return httpx.Response(201, json={"id": "inv_123"})
        return httpx.Response(201, json=body)
    return httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))


def _run(tmp_path, argv, *, env=None, incident=FakeIncident(), defaulted=(), log=None,
         http=None, keys_seen=None):
    base, rel = _snapshots(tmp_path)
    seen: dict = {}
    out: list[str] = []

    def capture_fn(domain, case, client, *, baseline, release, name):
        seen.update(domain=domain, case=case, client=client, baseline=baseline,
                    release=release, name=name)
        return incident, _trace(), list(defaulted)

    def factory(key):
        if keys_seen is not None:
            keys_seen.append(key)
        return "client"

    code = demo.main(["--baseline-snapshot", str(base), "--release-snapshot", str(rel), *argv],
                     env={"FLAGSMITH_SERVER_KEY_BENCH_1": "ser.fake"} if env is None else env,
                     client_factory=factory, capture_fn=capture_fn,
                     http=http if http is not None else _api(log if log is not None else []),
                     out=out.append)
    return code, out, seen


def test_captures_with_the_environment_key_and_posts_the_incident(tmp_path):
    log, keys = [], []
    code, out, seen = _run(tmp_path, ["--domain", "opspilot", "--environment", "bench-1"],
                           log=log, keys_seen=keys)
    assert code == 0
    assert keys == ["ser.fake"]
    assert seen["domain"] == "opspilot" and seen["case"] == "hero" and seen["client"] == "client"
    assert log == [("POST", "/api/incidents", {"id": "opspilot-hero-abc12345",
                                               "domain": "opspilot"})]
    assert "incident: opspilot-hero-abc12345" in out
    assert not any("ser.fake" in line for line in out)  # the key is never printed


def test_start_also_starts_the_investigation_in_the_given_mode(tmp_path):
    log = []
    code, out, _ = _run(tmp_path, ["--environment", "bench-1", "--start", "plain"], log=log)
    assert code == 0
    assert log[1] == ("POST", "/api/investigations",
                      {"incident_id": "opspilot-hero-abc12345", "mode": "plain"})
    assert "investigation: inv_123" in out


def test_no_violation_posts_nothing_and_exits_1(tmp_path):
    log = []
    code, out, _ = _run(tmp_path, ["--environment", "bench-1"], incident=None, log=log)
    assert code == 1 and log == []
    assert "no violation, nothing captured or posted" in out


def test_missing_key_exits_2_and_names_the_variable(tmp_path, capsys):
    code, _, seen = _run(tmp_path, ["--environment", "bench-1"], env={})
    assert code == 2 and seen == {}
    assert "FLAGSMITH_SERVER_KEY_BENCH_1 is not set" in capsys.readouterr().err


def test_default_handler_warning_is_shown(tmp_path):
    _, out, _ = _run(tmp_path, ["--environment", "bench-1"], defaulted=["agent_model"])
    assert any(line.startswith("WARNING: served by the default handler") for line in out)


def test_api_error_exits_2(tmp_path, capsys):
    failing = httpx.Client(base_url="http://api", transport=httpx.MockTransport(
        lambda r: httpx.Response(422, text="bad incident")))
    code, _, _ = _run(tmp_path, ["--environment", "bench-1"], http=failing)
    assert code == 2
    assert "POST /api/incidents returned HTTP 422" in capsys.readouterr().err


def test_name_is_passed_through(tmp_path):
    _, _, seen = _run(tmp_path, ["--environment", "bench-1", "--name", "opspilot-live-take"])
    assert seen["name"] == "opspilot-live-take"


def test_latest_snapshots_picks_the_newest_release_folder_with_both_files(tmp_path):
    env = tmp_path / "bench-1"
    for stamp, files in [("20261128T010000Z", ["baseline_env.json", "release_env.json"]),
                         ("20261128T020000Z", ["baseline_env.json", "release_env.json"]),
                         ("20261128T030000Z", ["pre_revert_env.json", "revert_env.json"])]:
        (env / stamp).mkdir(parents=True)
        for f in files:
            (env / stamp / f).write_text("{}")
    base, rel = demo.latest_snapshots("bench-1", tmp_path)
    assert base == env / "20261128T020000Z" / "baseline_env.json"
    assert rel == env / "20261128T020000Z" / "release_env.json"


def test_latest_snapshots_without_a_release_is_a_clear_error(tmp_path):
    with pytest.raises(demo.DemoError, match="no release snapshots"):
        demo.latest_snapshots("bench-1", tmp_path)
