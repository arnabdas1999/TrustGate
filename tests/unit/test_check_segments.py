"""check_segments: sandbox SEGMENTS vs the edge document, compared by behaviour."""

from __future__ import annotations

import httpx
import pytest

from sandbox.check_segments import CheckError, compare, fetch_document, main
from sandbox.flags import SEGMENTS


def doc(*segments):
    return {"project": {"segments": list(segments)}}


# Flagsmith's dashboard shape for "env = prod AND tier = 0": one ALL rule, two ANY children
FLAGSMITH_TIER0 = {"name": "tier0_production", "rules": [{"type": "ALL", "conditions": [],
                   "rules": [{"type": "ANY", "rules": [], "conditions": [
                       {"property_": "env", "operator": "EQUAL", "value": "prod"}]},
                             {"type": "ANY", "rules": [], "conditions": [
                       {"property_": "tier", "operator": "EQUAL", "value": "0"}]}]}]}


def test_same_rules_match():
    assert compare("opspilot", doc(FLAGSMITH_TIER0)) == []
    assert compare("vaultguard", doc(SEGMENTS["high_value_wires"])) == []


def test_equivalent_but_differently_written_rules_match():
    flat = {"name": "tier0_production", "rules": [{"type": "ALL", "rules": [], "conditions": [
        {"property_": "env", "operator": "EQUAL", "value": "prod"},
        {"property_": "tier", "operator": "EQUAL", "value": "0"}]}]}
    assert compare("opspilot", doc(flat)) == []


def test_or_instead_of_and_is_caught():
    either = {"name": "tier0_production", "rules": [{"type": "ANY", "rules": [], "conditions": [
        {"property_": "env", "operator": "EQUAL", "value": "prod"},
        {"property_": "tier", "operator": "EQUAL", "value": "0"}]}]}
    problems = compare("opspilot", doc(either))
    assert problems and "sandbox False, Flagsmith True" in problems[0]


def test_missing_or_renamed_segment_is_caught():
    renamed = {**FLAGSMITH_TIER0, "name": "tier0_prod"}
    assert "not in Flagsmith" in compare("opspilot", doc(renamed))[0]


def test_threshold_off_by_one_is_caught():
    gt = {"name": "high_value_wires", "rules": [{"type": "ALL", "conditions": [], "rules": [
        {"type": "ANY", "rules": [], "conditions": [
            {"property_": "amount_usd", "operator": "GREATER_THAN", "value": "10000"},
            {"property_": "new_vendor", "operator": "EQUAL", "value": "true"}]}]}]}
    assert any("10000" in p for p in compare("vaultguard", doc(gt)))


def test_fetch_and_cli(monkeypatch, capsys):
    def handler(request):
        assert request.headers["X-Environment-Key"] == "ser.test"
        return httpx.Response(200, json=doc(FLAGSMITH_TIER0))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_document("ser.test", client)["project"]["segments"][0]["name"] == \
        "tier0_production"
    with pytest.raises(CheckError):
        fetch_document("not-ser", client)
    monkeypatch.setenv("FLAGSMITH_SERVER_KEY_OPSPILOT_PROD", "ser.test")
    assert main(["--domain", "opspilot"], client) == 0
    assert "OK" in capsys.readouterr().out
    monkeypatch.delenv("FLAGSMITH_SERVER_KEY_OPSPILOT_PROD")
    assert main(["--domain", "opspilot"], client) == 2
