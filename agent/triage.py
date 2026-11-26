"""Deterministic triage: which Flagsmith segment does the incident's identity belong to?

Evaluates segment rules from the environment document (or the gate's canonical view of it)
on the incident's trusted traits (set by application code, never by users or the LLM).
No LLM involved.

Semantics follow Flagsmith's segment engine:
- A segment matches when it has rules and every top-level rule matches.
- A rule matches when its conditions match for its type (ALL: every condition, ANY: at
  least one, NONE: none) and every child rule matches. The dashboard's "Add AND Condition"
  makes sibling ANY rules under one ALL rule; "Or" adds conditions inside one ANY rule.
- Rule values are stored as strings and coerced to the trait's type. A value that can't be
  coerced makes the condition false. Operators are case-sensitive. A missing trait matches
  only IS_NOT_SET.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

TraitValue = bool | int | float | str

_TRUE, _FALSE = {"true", "True", "1"}, {"false", "False", "0"}
_ORDERING = {"GREATER_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN", "LESS_THAN_INCLUSIVE"}


class TriageError(Exception):
    pass


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compare(op: str, trait: Any, rule: str) -> bool:
    if isinstance(trait, bool):
        if op not in ("EQUAL", "NOT_EQUAL") or rule not in _TRUE | _FALSE:
            return False
        equal = trait == (rule in _TRUE)
        return equal if op == "EQUAL" else not equal

    left, right = _number(trait), _number(rule)
    numeric = isinstance(trait, int | float) or (op in _ORDERING and left is not None)
    if numeric:
        if left is None or right is None:
            return False
        a, b = left, right
    else:
        a, b = str(trait), rule

    match op:
        case "EQUAL":
            return a == b
        case "NOT_EQUAL":
            return a != b
        case "GREATER_THAN":
            return a > b
        case "GREATER_THAN_INCLUSIVE":
            return a >= b
        case "LESS_THAN":
            return a < b
        case "LESS_THAN_INCLUSIVE":
            return a <= b
    return False


def _condition(cond: Mapping[str, Any], traits: Mapping[str, TraitValue]) -> bool:
    prop = cond.get("property_", cond.get("property"))
    op = cond.get("operator")
    rule = "" if cond.get("value") is None else str(cond.get("value"))
    present = prop in traits and traits[prop] is not None
    if op == "IS_SET":
        return present
    if op == "IS_NOT_SET":
        return not present
    if not present:
        return False
    trait = traits[prop]
    if op == "IN":
        return str(trait) in {v.strip() for v in rule.split(",")}
    if op == "CONTAINS":
        return rule in str(trait)
    if op == "NOT_CONTAINS":
        return rule not in str(trait)
    if op == "REGEX":
        try:
            return re.search(rule, str(trait)) is not None
        except re.error:
            return False
    return _compare(str(op), trait, rule)


def _rule(rule: Mapping[str, Any], traits: Mapping[str, TraitValue]) -> bool:
    results = [_condition(c, traits) for c in rule.get("conditions") or []]
    kind = rule.get("type")
    if kind == "ALL":
        own = all(results)
    elif kind == "ANY":
        own = any(results) if results else True
    elif kind == "NONE":
        own = not any(results)
    else:
        return False
    return own and all(_rule(child, traits) for child in rule.get("rules") or [])


def segment_matches(segment: Mapping[str, Any], traits: Mapping[str, TraitValue]) -> bool:
    rules = segment.get("rules") or []
    return bool(rules) and all(_rule(r, traits) for r in rules)


def segments_of(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The segment definitions ({name, rules}) from either document shape:
    - the edge environment document: {"project": {"segments": [{"name", "rules"}, ...]}}
    - the gate's canonical view (gate.fingerprint.canonical_view):
      {"features": {...}, "segments": {name: {"rules": [...], "overrides": {...}}}}
    """
    segments = document.get("segments")
    if isinstance(segments, Mapping):
        return [{"name": name, "rules": (seg or {}).get("rules") or []}
                for name, seg in segments.items()]
    return list((document.get("project") or {}).get("segments") or [])


def matching_segments(document: Mapping[str, Any], traits: Mapping[str, TraitValue]) -> list[str]:
    return sorted(s["name"] for s in segments_of(document) if segment_matches(s, traits))


def triage(document: Mapping[str, Any], traits: Mapping[str, TraitValue]) -> str | None:
    """The one segment the identity belongs to, None if it belongs to none."""
    if not isinstance(traits, Mapping) or not all(
            isinstance(v, bool | int | float | str) or v is None for v in traits.values()):
        raise TriageError("traits must be plain values set by application code")
    matches = matching_segments(document, traits)
    if len(matches) > 1:
        raise TriageError(f"identity is in more than one segment: {matches}")
    return matches[0] if matches else None
