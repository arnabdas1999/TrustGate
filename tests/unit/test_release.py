import json

import httpx
import pytest

from scripts.release import (
    PLAN_RELEASE,
    AdminApi,
    ReleaseError,
    apply_values,
    check_environment_allowed,
    plan_changes,
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


def _doc(values, env_id=121197):
    return {"id": env_id, "api_key": "dev-client-key", "name": "Development",
            "feature_states": [{"enabled": True, "feature": {"name": k}, "feature_state_value": v}
                               for k, v in values.items()], "project": {"segments": []}}


class FakeFlagsmith:
    def __init__(self):
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
            return httpx.Response(200, json=_doc(BASELINE))
        if path == "/api/v1/environment-document/":  # edge document: ints come back as strings
            return httpx.Response(200, json=_doc(EDGE_BASELINE))
        if path.endswith("/versions/") and request.method == "POST":
            self.versions.append((path, json.loads(request.content)))
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
