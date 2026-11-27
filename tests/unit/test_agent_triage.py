import pytest

from agent.triage import TriageError, matching_segments, segment_matches, triage


def _cond(prop, op, value, key="property_"):
    return {key: prop, "operator": op, "value": value}


TIER0 = {"name": "tier0_production", "rules": [{"type": "ALL", "conditions": [], "rules": [
    {"type": "ANY", "rules": [], "conditions": [_cond("env", "EQUAL", "prod")]},
    {"type": "ANY", "rules": [], "conditions": [_cond("tier", "EQUAL", "0")]},
]}]}

# One ANY rule with two conditions: amount_usd >= 10000 OR new_vendor == true
HIGH_VALUE = {"name": "high_value_wires", "rules": [{"type": "ALL", "conditions": [], "rules": [
    {"type": "ANY", "rules": [], "conditions": [
        _cond("amount_usd", "GREATER_THAN_INCLUSIVE", "10000"),
        _cond("new_vendor", "EQUAL", "true")]},
]}]}


@pytest.mark.parametrize(("traits", "expected"), [
    ({"env": "prod", "tier": 0}, True),
    ({"env": "prod", "tier": "0"}, True),
    ({"env": "prod", "tier": 1}, False),
    ({"env": "staging", "tier": 0}, False),
    ({"env": "staging", "tier": 2}, False),
    ({"env": "Prod", "tier": 0}, False),  # Flagsmith operators are case-sensitive
    ({"env": "prod"}, False),  # a missing trait never matches EQUAL
    ({}, False),
])
def test_tier0_production_needs_both(traits, expected):
    assert segment_matches(TIER0, traits) is expected


@pytest.mark.parametrize(("traits", "expected"), [
    ({"amount_usd": 25000, "new_vendor": False}, True),
    ({"amount_usd": 10000, "new_vendor": False}, True),
    ({"amount_usd": 9999.99, "new_vendor": False}, False),
    ({"amount_usd": 500, "new_vendor": True}, True),
    ({"amount_usd": 500, "new_vendor": "true"}, True),
    ({"amount_usd": "25000", "new_vendor": False}, True),
    ({"amount_usd": 500}, False),
    ({}, False),
])
def test_high_value_wires_is_either(traits, expected):
    assert segment_matches(HIGH_VALUE, traits) is expected


@pytest.mark.parametrize(("op", "rule_value", "trait", "expected"), [
    ("NOT_EQUAL", "prod", "staging", True),
    ("NOT_EQUAL", "prod", "prod", False),
    ("GREATER_THAN", "5", 6, True),
    ("GREATER_THAN", "5", 5, False),
    ("LESS_THAN", "5", 4, True),
    ("LESS_THAN_INCLUSIVE", "5", 5, True),
    ("IN", "prod,staging", "staging", True),
    ("IN", "prod,staging", "dev", False),
    ("CONTAINS", "pay", "payments-prod", True),
    ("NOT_CONTAINS", "pay", "payments-prod", False),
    ("REGEX", r"^payments-.*", "payments-prod", True),
    ("IS_SET", "", "anything", True),
    ("GREATER_THAN", "abc", 5, False),  # can't coerce -> false, like Flagsmith
])
def test_operators(op, rule_value, trait, expected):
    seg = {"name": "s", "rules": [{"type": "ALL", "rules": [],
                                   "conditions": [_cond("t", op, rule_value)]}]}
    assert segment_matches(seg, {"t": trait}) is expected


def test_is_not_set_matches_a_missing_trait():
    seg = {"name": "s", "rules": [{"type": "ALL", "rules": [],
                                   "conditions": [_cond("t", "IS_NOT_SET", "")]}]}
    assert segment_matches(seg, {}) is True
    assert segment_matches(seg, {"t": 1}) is False


def test_none_rule_type():
    seg = {"name": "s", "rules": [{"type": "NONE", "rules": [],
                                   "conditions": [_cond("env", "EQUAL", "prod")]}]}
    assert segment_matches(seg, {"env": "staging"}) is True
    assert segment_matches(seg, {"env": "prod"}) is False


def test_unknown_operator_never_matches():
    seg = {"name": "s", "rules": [{"type": "ALL", "rules": [],
                                   "conditions": [_cond("t", "PERCENTAGE_SPLIT", "50")]}]}
    assert segment_matches(seg, {"t": 1}) is False


def test_api_shape_property_key_works_too():
    seg = {"name": "s", "rules": [{"type": "ALL", "rules": [],
                                   "conditions": [_cond("env", "EQUAL", "prod", key="property")]}]}
    assert segment_matches(seg, {"env": "prod"})


def test_matching_segments_from_an_edge_document():
    doc = {"project": {"segments": [TIER0, HIGH_VALUE]}}
    assert matching_segments(doc, {"env": "prod", "tier": 0}) == ["tier0_production"]
    assert matching_segments(doc, {"env": "staging", "tier": 2}) == []


def test_triage_returns_the_single_match_or_explains():
    doc = {"project": {"segments": [TIER0, HIGH_VALUE]}}
    assert triage(doc, {"env": "prod", "tier": 0}) == "tier0_production"
    assert triage(doc, {"env": "staging", "tier": 2}) is None
    both = {"env": "prod", "tier": 0, "amount_usd": 20000}
    with pytest.raises(TriageError, match="more than one"):
        triage(doc, both)


def test_traits_must_be_a_dict_of_plain_values():
    with pytest.raises(TriageError):
        triage({"project": {"segments": [TIER0]}}, {"env": {"nested": 1}})


def _with_ids(rule, start=1):
    """An edge-document rule: ids on rules and conditions, as Flagsmith sends them."""
    return {**rule, "id": start,
            "conditions": [{**c, "id": start * 10 + i} for i, c in enumerate(rule["conditions"])],
            "rules": [_with_ids(r, start * 10 + i) for i, r in enumerate(rule["rules"])]}


def test_triage_reads_the_gate_canonical_view():
    from gate.fingerprint import canonical_view

    doc = {"id": 1, "name": "OpsPilot Prod",
           "feature_states": [{"enabled": True, "feature": {"name": "agent_model"},
                               "feature_state_value": "model_cheap"}],
           "project": {"segments": [
               {"id": 7, "name": seg["name"], "rules": [_with_ids(r) for r in seg["rules"]],
                "feature_states": []} for seg in (TIER0, HIGH_VALUE)]}}
    view = canonical_view(doc)
    assert isinstance(view["segments"], dict) and "project" not in view
    assert triage(view, {"env": "prod", "tier": 0}) == "tier0_production"
    assert triage(view, {"env": "prod", "tier": 1}) is None
    assert triage(view, {"env": "staging", "tier": 0}) is None
    assert matching_segments(view, {"amount_usd": 20000}) == ["high_value_wires"]
    # the same document, unconverted, gives the same answers
    assert triage(doc, {"env": "prod", "tier": 0}) == "tier0_production"


def test_canonical_view_without_segments_matches_nothing():
    assert matching_segments({"features": {}, "segments": {}}, {"env": "prod", "tier": 0}) == []
