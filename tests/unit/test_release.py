import json
import shlex

import httpx
import pytest

import scripts.release as release
from scripts.release import (
    PLAN_RELEASE,
    AdminApi,
    ReleaseError,
    apply_values,
    check_environment_allowed,
    plan_changes,
    revert_target,
    snapshot,
    typed_value,
)

ENVS = [{"id": 121197, "name": "Development", "api_key": "dev-client-key"},
        {"id": 121198, "name": "Demo Production", "api_key": "prod-client-key"}]
FEATURES = [{"id": 306125 + i, "name": n} for i, n in enumerate(
    ["agent_model", "x", "agent_prompt_version", "agent_tool_policy", "agent_max_steps",
     "agent_context_mode", "agent_temperature", "agent_safe_fallback"])]
BASELINE = {"agent_model": "model_flagship", "agent_prompt_version": "v1_strict",
            "agent_tool_policy": "restricted", "agent_max_steps": 6,
            "agent_context_mode": "summary", "agent_temperature": "0.0",
            "agent_safe_fallback": False}


EDGE_BASELINE = {**BASELINE, "agent_max_steps": "6"}


SEGMENTS = [{"name": "tier0_production", "rules": [{"type": "ALL", "conditions": [],
                                                     "rules": []}],
             "feature_states": [{"enabled": True, "feature": {"name": "agent_model"},
                                 "feature_state_value": "model_flagship"}]}]


def _doc(values, env_id=121197, enabled=None):
    enabled = enabled or {}
    return {"id": env_id, "api_key": "dev-client-key", "name": "Development",
            "feature_states": [{"enabled": enabled.get(k, True), "feature": {"name": k},
                                "feature_state_value": v} for k, v in values.items()],
            "project": {"segments": SEGMENTS, "hide_disabled_flags": False,
                        "organisation": {"name": "Lazy Hackers", "persist_trait_data": True}},
            "identity_overrides": [{"identifier": "payments-prod", "identity_traits": [],
                                    "identity_features": []}],
            "allow_client_traits": True, "hide_sensitive_data": False,
            "webhook_config": {"url": "https://example.invalid/hook", "secret": "whsec"},
            "amplitude_config": {"api_key": "amp-secret"}}


class FakeFlagsmith:
    """Serves the admin and edge documents and applies published versions to its state."""

    def __init__(self, values=None, enabled=None, env_id=121197):
        self.values = dict(BASELINE if values is None else values)
        self.enabled = dict(enabled or {})
        self.env_id = env_id
        self.versions = []
        self.headers = []

    def __call__(self, request):
        self.headers.append(dict(request.headers))
        path = request.url.path
        if path == "/api/v1/environments/" and request.method == "GET":
            return httpx.Response(200, json={"results": ENVS})
        if path == "/api/v1/projects/56664/features/":
            return httpx.Response(200, json={"results": FEATURES})
        if path.endswith("/document/"):  # admin document: typed values
            return httpx.Response(200, json=_doc(self.values, self.env_id, self.enabled))
        if path == "/api/v1/environment-document/":  # edge document: ints come back as strings
            edge = {k: str(v) if isinstance(v, int) and not isinstance(v, bool) else v
                    for k, v in self.values.items()}
            return httpx.Response(200, json=_doc(edge, self.env_id, self.enabled))
        if path.endswith("/versions/") and request.method == "POST":
            body = json.loads(request.content)
            self.versions.append((path, body))
            for fs in body["feature_states_to_update"]:
                name = next(f["name"] for f in FEATURES if f["id"] == fs["feature"])
                typed = fs["feature_state_value"]
                self.values[name] = next(v for k, v in typed.items() if k.endswith("_value"))
                self.enabled[name] = fs["enabled"]
            return httpx.Response(201, json={"uuid": f"v-{len(self.versions)}"})
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeFlagsmith()


@pytest.fixture
def api(fake):
    return AdminApi("pre.fixsecret", project_id=56664,
                    client=httpx.Client(transport=httpx.MockTransport(fake),
                                        base_url="https://api.flagsmith.com"))


def test_plan_release_is_the_frozen_plan():
    assert PLAN_RELEASE == {"agent_model": "model_cheap", "agent_prompt_version": "v2_fast",
                            "agent_tool_policy": "expanded", "agent_max_steps": 12,
                            "agent_context_mode": "full_history"}


def test_plan_changes_lists_only_real_changes():
    changes = plan_changes(BASELINE, PLAN_RELEASE)
    assert [(c.flag, c.old, c.new) for c in changes] == [
        ("agent_context_mode", "summary", "full_history"),
        ("agent_max_steps", 6, 12),
        ("agent_model", "model_flagship", "model_cheap"),
        ("agent_prompt_version", "v1_strict", "v2_fast"),
        ("agent_tool_policy", "restricted", "expanded")]
    assert plan_changes({**BASELINE, **PLAN_RELEASE}, PLAN_RELEASE) == []


def test_plan_changes_treats_string_and_int_alike():
    assert plan_changes({**BASELINE, "agent_max_steps": "12"}, {"agent_max_steps": 12}) == []
    assert plan_changes(EDGE_BASELINE, {"agent_max_steps": 6}) == []


def test_plan_changes_refuses_unknown_flags():
    with pytest.raises(ReleaseError, match="not in the environment"):
        plan_changes(BASELINE, {"no_such_flag": "x"})


@pytest.mark.parametrize(("value", "expected"), [
    ("v2_fast", {"type": "unicode", "string_value": "v2_fast"}),
    (12, {"type": "int", "integer_value": 12}),
    (True, {"type": "bool", "boolean_value": True}),
    (0.5, {"type": "unicode", "string_value": "0.5"}),
])
def test_typed_value(value, expected):
    assert typed_value(value) == expected


def test_apply_posts_one_published_version_per_changed_flag(api, fake):
    changes = plan_changes(BASELINE, PLAN_RELEASE)
    uuids = apply_values(api, "Development", changes, note="test release")
    assert len(uuids) == 5 and len(fake.versions) == 5
    path, body = fake.versions[1]
    assert path == "/api/v1/environments/121197/features/306129/versions/"
    assert body == {"publish_immediately": True, "description": "release.py: test release",
                    "feature_states_to_update": [{
                        "feature": 306129, "enabled": True, "feature_segment": None,
                        "feature_state_value": {"type": "int", "integer_value": 12}}]}
    assert all(h["authorization"] == "Api-Key pre.fixsecret" for h in fake.headers)


def test_snapshot_keeps_both_documents_without_keys(api, fake):
    snap = snapshot(api, "Development", server_key="ser.abc",
                    edge_client=httpx.Client(transport=httpx.MockTransport(fake),
                                             base_url="https://edge.api.flagsmith.com"))
    assert snap["environment"] == "Development" and snap["environment_id"] == 121197
    assert snap["values"] == BASELINE  # typed, from the admin document
    assert snap["values"]["agent_max_steps"] == 6
    assert snap["edge_values"]["agent_max_steps"] == "6"
    assert snap["admin_values_match"] is True  # same values, loosely compared
    text = json.dumps(snap)
    assert "dev-client-key" not in text and "ser.abc" not in text


def test_snapshot_refuses_a_server_key_for_another_environment(api, fake):
    def other_env(request):
        if request.url.path == "/api/v1/environment-document/":
            return httpx.Response(200, json=_doc(BASELINE, env_id=999))
        return fake(request)

    with pytest.raises(ReleaseError, match="another environment"):
        snapshot(api, "Development", server_key="ser.abc",
                 edge_client=httpx.Client(transport=httpx.MockTransport(other_env),
                                          base_url="https://edge.api.flagsmith.com"))


@pytest.mark.parametrize("env", ["Demo Production", "bench-1", "Production"])
def test_other_environments_need_explicit_confirmation(env):
    with pytest.raises(ReleaseError, match="confirm"):
        check_environment_allowed(env, confirm=None)
    with pytest.raises(ReleaseError, match="confirm"):
        check_environment_allowed(env, confirm="Development")
    check_environment_allowed(env, confirm=env)


def test_development_needs_no_confirmation():
    check_environment_allowed("Development", confirm=None)


def test_unknown_environment_is_refused(api):
    with pytest.raises(ReleaseError, match="unknown environment"):
        api.environment("Staging")


# --- enabled, safety check inside apply_values, trimmed snapshots, main() ------------------


def _edge_client(fake):
    return httpx.Client(transport=httpx.MockTransport(fake),
                        base_url="https://edge.api.flagsmith.com")


def test_publish_value_sends_enabled(api, fake):
    api.publish_value(121197, "agent_max_steps", 6, "x", enabled=False)
    body = fake.versions[0][1]["feature_states_to_update"][0]
    assert body["enabled"] is False
    assert body["feature_state_value"] == {"type": "int", "integer_value": 6}


@pytest.mark.parametrize("confirm", [None, "Development", "demo production"])
def test_apply_values_refuses_other_environments_without_confirmation(api, fake, confirm):
    changes = plan_changes(BASELINE, PLAN_RELEASE)
    with pytest.raises(ReleaseError, match="confirm"):
        apply_values(api, "Demo Production", changes, note="x", confirm=confirm)
    assert fake.versions == [] and fake.headers == []  # refused before any request


def test_apply_values_writes_a_confirmed_environment(api, fake):
    changes = plan_changes(BASELINE, PLAN_RELEASE)
    apply_values(api, "Demo Production", changes, note="x", confirm="Demo Production")
    assert len(fake.versions) == 5
    assert all(p.startswith("/api/v1/environments/121198/") for p, _ in fake.versions)


def test_apply_values_restores_enabled_per_flag(api, fake):
    changes = plan_changes(BASELINE, {"agent_model": "model_cheap", "agent_max_steps": 12})
    apply_values(api, "Development", changes, note="x", enabled={"agent_max_steps": False})
    sent = {b["feature_states_to_update"][0]["feature"]: b["feature_states_to_update"][0]["enabled"]
            for _, b in fake.versions}
    assert sent == {306125: True, 306129: False}  # unlisted flags stay enabled


def test_snapshot_captures_enabled(api):
    fake = FakeFlagsmith(enabled={"agent_safe_fallback": False})
    api._http = httpx.Client(transport=httpx.MockTransport(fake),
                             base_url="https://api.flagsmith.com")
    snap = snapshot(api, "Development", server_key="ser.abc", edge_client=_edge_client(fake))
    assert snap["enabled"]["agent_safe_fallback"] is False
    assert snap["enabled"]["agent_model"] is True
    assert set(snap["enabled"]) == set(BASELINE)


def test_snapshot_keeps_only_feature_states_and_segments(api, fake):
    snap = snapshot(api, "Development", server_key="ser.abc", edge_client=_edge_client(fake))
    for name in ("edge_document", "admin_document"):
        doc = snap[name]
        assert set(doc) == {"id", "name", "feature_states", "project"}, name
        assert doc["id"] == 121197 and doc["name"] == "Development"
        assert doc["project"] == {"segments": SEGMENTS}
        assert len(doc["feature_states"]) == len(BASELINE)
    text = json.dumps(snap)
    for secret in ("payments-prod", "whsec", "amp-secret", "dev-client-key", "Lazy Hackers"):
        assert secret not in text


def test_plan_changes_with_enabled_lists_enabled_only_changes():
    target = {"agent_model": "model_flagship", "agent_max_steps": 6}
    assert plan_changes(BASELINE, target) == []
    changes = plan_changes(BASELINE, target, enabled={"agent_max_steps": False},
                           current_enabled={"agent_max_steps": True})
    assert [(c.flag, c.old, c.new) for c in changes] == [("agent_max_steps", 6, 6)]
    assert plan_changes(BASELINE, target, enabled={"agent_max_steps": False},
                        current_enabled={"agent_max_steps": False}) == []


def test_revert_target_takes_values_and_enabled_from_the_baseline():
    saved = {"environment": "Development", "values": BASELINE,
             "enabled": {**dict.fromkeys(BASELINE, True), "agent_context_mode": False}}
    values, enabled = revert_target(saved, "Development")
    assert values == {k: BASELINE[k] for k in PLAN_RELEASE}
    assert enabled == {**dict.fromkeys(PLAN_RELEASE, True), "agent_context_mode": False}
    with pytest.raises(ReleaseError, match="another environment"):
        revert_target(saved, "Demo Production")


def test_revert_target_reads_enabled_from_an_older_baseline():
    # Baselines saved before `enabled` was captured still have the admin document.
    old = {"environment": "Development", "values": BASELINE,
           "admin_document": _doc(BASELINE, enabled={"agent_model": False})}
    _, enabled = revert_target(old, "Development")
    assert enabled["agent_model"] is False and enabled["agent_max_steps"] is True


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """main() against a FakeFlagsmith: no network, no real keys."""
    real_snapshot = release.snapshot

    def run(fake, *args):
        monkeypatch.setenv("FLAGSMITH_API_KEY", "pre.fake")
        monkeypatch.setenv("FLAGSMITH_SERVER_KEY", "ser.fake")
        client = httpx.Client(transport=httpx.MockTransport(fake),
                              base_url="https://api.flagsmith.com")
        monkeypatch.setattr(release, "AdminApi",
                            lambda key, project_id: AdminApi(key, project_id=project_id,
                                                             client=client))
        monkeypatch.setattr(release, "snapshot", lambda api, env, server_key: real_snapshot(
            api, env, server_key=server_key, edge_client=_edge_client(fake)))
        return release.main(["--project-id", "56664", "--out", str(tmp_path), *args])

    return run


def test_main_release_then_revert_restores_values_and_enabled(run_main, tmp_path):
    fake = FakeFlagsmith(enabled={"agent_context_mode": False})
    assert run_main(fake, "--environment", "Development") == 0
    assert {k: fake.values[k] for k in PLAN_RELEASE} == PLAN_RELEASE
    assert fake.enabled["agent_context_mode"] is True  # the forward release enables
    baseline = next(tmp_path.glob("Development/*/baseline_env.json"))
    assert json.loads(baseline.read_text())["enabled"]["agent_context_mode"] is False

    fake.versions.clear()
    assert run_main(fake, "--environment", "Development", "--revert", str(baseline)) == 0
    assert fake.values == BASELINE
    assert fake.enabled["agent_context_mode"] is False
    assert len(fake.versions) == 5


def test_main_passes_the_confirmation_to_apply_values(run_main, monkeypatch):
    seen = {}
    real_apply = release.apply_values

    def spy(api, environment, changes, **kwargs):
        seen.update(kwargs)
        return real_apply(api, environment, changes, **kwargs)

    monkeypatch.setattr(release, "apply_values", spy)
    fake = FakeFlagsmith(env_id=121198)
    assert run_main(fake, "--environment", "Demo Production",
                    "--confirm-environment", "Demo Production") == 0
    assert seen["confirm"] == "Demo Production"
    assert all(p.startswith("/api/v1/environments/121198/") for p, _ in fake.versions)


def test_main_refuses_an_unconfirmed_environment(run_main):
    fake = FakeFlagsmith(env_id=121198)
    assert run_main(fake, "--environment", "Demo Production") == 2
    assert fake.versions == []


class FailsMidRelease(FakeFlagsmith):
    """Publishes the first two versions, then the network drops."""

    def __call__(self, request):
        if request.url.path.endswith("/versions/") and len(self.versions) >= 2:
            raise httpx.ConnectError("connection reset", request=request)
        return super().__call__(request)


def test_main_half_applied_release_prints_the_exact_revert_command(run_main, tmp_path, capsys):
    fake = FailsMidRelease()
    assert run_main(fake, "--environment", "Development") == 2
    assert len(fake.versions) == 2  # half-changed, the case the review raised
    baseline = next(tmp_path.glob("Development/*/baseline_env.json"))
    err = capsys.readouterr().err
    assert "Flagsmith call failed: ConnectError" in err
    # shlex.quote wraps Windows paths (backslashes) in quotes, so compare against the same
    # quoting the script uses rather than the bare path.
    assert ("uv run python -m scripts.release --environment Development --revert "
            f"{shlex.quote(str(baseline))}") in err

    fake.versions.clear()  # the printed command puts every flag back
    healthy = FakeFlagsmith(values=fake.values, enabled=fake.enabled)
    assert run_main(healthy, "--environment", "Development", "--revert", str(baseline)) == 0
    assert healthy.values == BASELINE


def test_main_half_applied_revert_says_to_run_the_same_revert(run_main, tmp_path, capsys):
    base = FakeFlagsmith()
    assert run_main(base, "--environment", "Development") == 0
    baseline = next(tmp_path.glob("Development/*/baseline_env.json"))
    broken = FailsMidRelease(values=base.values)
    assert run_main(broken, "--environment", "Development", "--revert", str(baseline)) == 2
    err = capsys.readouterr().err
    assert "To finish the revert" in err and f"--revert {shlex.quote(str(baseline))}" in err


def test_main_failure_before_any_write_prints_no_revert_hint(run_main, capsys):
    class Down(FakeFlagsmith):
        def __call__(self, request):
            raise httpx.ConnectError("down", request=request)
    assert run_main(Down(), "--environment", "Development") == 2
    err = capsys.readouterr().err
    assert "Flagsmith call failed" in err and "--revert" not in err
