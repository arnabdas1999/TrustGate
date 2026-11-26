import copy

import httpx
import pytest

from gate.fingerprint import (
    EDGE_DOCUMENT_URL,
    FingerprintError,
    canonical_view,
    fetch_environment_document,
    fingerprint,
)


def _fs(name, value, *, fid, enabled=True, priority=None, uuid="u"):
    return {
        "django_id": fid * 10, "featurestate_uuid": f"{uuid}-{fid}", "enabled": enabled,
        "feature": {"id": fid, "name": name, "type": "STANDARD"},
        "feature_segment": None if priority is None else {"priority": priority},
        "feature_state_value": value, "multivariate_feature_state_values": [],
    }


def _doc():
    """Same shape as the live edge document (keys checked against Flagsmith on Nov 26)."""
    return {
        "allow_client_traits": True, "api_key": "client-key", "hide_disabled_flags": None,
        "hide_sensitive_data": False, "id": 121197, "identity_overrides": [], "name": "Development",
        "updated_at": "2026-11-26T18:20:27Z", "use_identity_composite_key_for_hashing": True,
        "use_identity_overrides_in_local_eval": False,
        "feature_states": [
            _fs("agent_prompt_version", "v1_strict", fid=1),
            _fs("agent_max_steps", 6, fid=2),
            _fs("agent_safe_fallback", False, fid=3),
        ],
        "project": {
            "enable_realtime_updates": False, "hide_disabled_flags": False, "id": 56664,
            "name": "OpsPilot", "organisation": {"id": 9, "name": "org"},
            "server_key_only_feature_ids": [],
            "segments": [{
                "id": 1205876, "name": "tier0_production", "feature_states": [],
                "rules": [{"type": "ALL", "conditions": [], "rules": [
                    {"type": "ANY", "rules": [], "conditions": [
                        {"operator": "EQUAL", "property_": "env", "value": "prod"}]},
                    {"type": "ANY", "rules": [], "conditions": [
                        {"operator": "EQUAL", "property_": "tier", "value": "0"}]},
                ]}],
            }],
        },
    }


def _with_override(doc, value="v2_fast", priority=0):
    doc["project"]["segments"][0]["feature_states"] = [
        _fs("agent_prompt_version", value, fid=1, priority=priority, uuid="seg")]
    return doc


BASE = fingerprint(_doc())


def test_fingerprint_is_hex_sha256():
    assert len(BASE) == 64 and int(BASE, 16) >= 0


def test_same_document_same_fingerprint():
    assert fingerprint(_doc()) == BASE


def test_volatile_fields_and_order_do_not_matter():
    doc = _doc()
    doc["updated_at"] = "2030-01-01T00:00:00Z"
    doc["api_key"] = "rotated"
    doc["feature_states"].reverse()
    for fs in doc["feature_states"]:
        fs["django_id"] += 1000
        fs["featurestate_uuid"] = "new"
        fs["feature"]["id"] += 1000
    rules = doc["project"]["segments"][0]["rules"][0]["rules"]
    rules.reverse()
    doc["project"]["segments"][0]["id"] = 1
    doc["project"]["server_key_only_feature_ids"] = [1]
    assert fingerprint(doc) == BASE


def test_identity_overrides_are_out_of_scope():
    doc = _doc()
    doc["identity_overrides"] = [{"identifier": "x", "identity_features": []}]
    assert fingerprint(doc) == BASE


@pytest.mark.parametrize("mutate", [
    lambda d: d["feature_states"][0].update(feature_state_value="v2_fast"),
    lambda d: d["feature_states"][1].update(feature_state_value=12),
    lambda d: d["feature_states"][2].update(enabled=False),
    lambda d: d["feature_states"].pop(),
    lambda d: d["feature_states"].append(_fs("agent_temperature", "0.0", fid=4)),
    lambda d: d["project"]["segments"][0]["rules"][0]["rules"][1]["conditions"][0]
        .update(value="1"),
    lambda d: d["project"]["segments"][0]["rules"][0]["rules"][0]["conditions"][0]
        .update(operator="NOT_EQUAL"),
    lambda d: d["project"]["segments"][0]["rules"][0].update(type="ANY"),
    lambda d: d["project"]["segments"][0].update(name="tier0_prod"),
    lambda d: _with_override(d),
], ids=["flag value", "int value", "enabled", "flag removed", "flag added", "rule value",
        "rule operator", "AND becomes OR", "segment renamed", "override added"])
def test_real_changes_change_the_fingerprint(mutate):
    doc = _doc()
    mutate(doc)
    assert fingerprint(doc) != BASE


def test_override_value_and_priority_changes_are_seen():
    with_override = fingerprint(_with_override(_doc()))
    assert fingerprint(_with_override(_doc(), value="v1_strict")) != with_override
    assert fingerprint(_with_override(_doc(), priority=1)) != with_override
    removed = _with_override(_doc())
    removed["project"]["segments"][0]["feature_states"] = []
    assert fingerprint(removed) == BASE


def test_string_and_int_values_are_not_confused():
    doc = _doc()
    doc["feature_states"][1]["feature_state_value"] = "6"
    assert fingerprint(doc) != BASE


def test_canonical_view_has_only_flags_and_segments():
    view = canonical_view(_with_override(_doc()))
    assert set(view) == {"features", "segments"}
    assert view["features"]["agent_prompt_version"] == {
        "enabled": True, "value": "v1_strict", "multivariate": []}
    assert view["segments"]["tier0_production"]["overrides"]["agent_prompt_version"]["value"] \
        == "v2_fast"


@pytest.mark.parametrize("bad", [{}, {"feature_states": "x"}, [], None])
def test_malformed_documents_are_refused(bad):
    with pytest.raises(FingerprintError):
        fingerprint(bad)


def test_document_does_not_mutate_input():
    doc = _doc()
    before = copy.deepcopy(doc)
    fingerprint(doc)
    assert doc == before


# --- fetching the edge document ---------------------------------------------------------


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_sends_server_key_to_edge_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-Environment-Key")
        return httpx.Response(200, json=_doc())

    doc = fetch_environment_document("ser.abc123", client=_client(handler))
    assert doc["name"] == "Development"
    assert seen == {"url": EDGE_DOCUMENT_URL, "key": "ser.abc123"}


def test_fetch_refuses_non_server_keys():
    with pytest.raises(FingerprintError, match="server-side"):
        fetch_environment_document("client-side-key", client=_client(lambda r: httpx.Response(200)))


def test_fetch_error_does_not_leak_the_key():
    def handler(request):
        return httpx.Response(401, text="bad key ser.abc123")

    with pytest.raises(FingerprintError) as err:
        fetch_environment_document("ser.abc123", client=_client(handler))
    assert "ser.abc123" not in str(err.value)
    assert "401" in str(err.value)
