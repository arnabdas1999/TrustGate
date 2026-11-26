"""Flag sets and segment definitions the sandbox replays against.

BASELINE and RELEASE follow the frozen plan (p.5). Segment rules use Flagsmith's rule shape so
the sandbox applies a segment override with the same engine as triage (agent.triage).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.triage import segment_matches
from contracts.models import FlagConfig, FlagValue, Identity

BASELINE: dict[str, FlagValue] = {
    "agent_model": "model_flagship",
    "agent_prompt_version": "v1_strict",
    "agent_tool_policy": "restricted",
    "agent_max_steps": 6,
    "agent_context_mode": "summary",
    "agent_temperature": "0.0",
    "agent_safe_fallback": False,
}

# The 5-flag release (scripts/release.py PLAN_RELEASE); temperature and fallback unchanged.
RELEASE_CHANGES: dict[str, FlagValue] = {
    "agent_model": "model_cheap",
    "agent_prompt_version": "v2_fast",
    "agent_tool_policy": "expanded",
    "agent_max_steps": 12,
    "agent_context_mode": "full_history",
}
RELEASE: dict[str, FlagValue] = {**BASELINE, **RELEASE_CHANGES}


def _all_of(*conditions: tuple[str, str, str]) -> dict[str, Any]:
    """Flagsmith shape for "every condition": one ALL rule with an ANY child per condition."""
    return {"type": "ALL", "conditions": [], "rules": [
        {"type": "ANY", "conditions": [{"property_": p, "operator": op, "value": v}],
         "rules": []} for p, op, v in conditions]}


SEGMENTS: dict[str, dict[str, Any]] = {
    "tier0_production": {"name": "tier0_production",
                         "rules": [_all_of(("env", "EQUAL", "prod"), ("tier", "EQUAL", "0"))]},
    "high_value_wires": {"name": "high_value_wires", "rules": [{
        "type": "ALL", "conditions": [], "rules": [{"type": "ANY", "rules": [], "conditions": [
            {"property_": "amount_usd", "operator": "GREATER_THAN_INCLUSIVE", "value": "10000"},
            {"property_": "new_vendor", "operator": "EQUAL", "value": "true"},
        ]}]}]},
}


def config(values: Mapping[str, FlagValue], source: str = "override",
           segment_overrides: Mapping[str, Mapping[str, FlagValue]] | None = None) -> FlagConfig:
    return FlagConfig(values=dict(values), source=source,
                      segment_overrides={k: dict(v) for k, v in (segment_overrides or {}).items()})


def baseline_config() -> FlagConfig:
    return config(BASELINE, "baseline")


def release_config() -> FlagConfig:
    return config(RELEASE, "captured")


def effective_values(flags: FlagConfig, identity: Identity,
                     segments: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]
                     = SEGMENTS) -> dict[str, FlagValue]:
    """The values this identity is served: environment values, then any segment override whose
    rules match the identity's traits, then the agent_safe_fallback kill switch."""
    by_name = segments if isinstance(segments, Mapping) else {s["name"]: s for s in segments}
    values = {**BASELINE, **flags.values}
    for name, overrides in flags.segment_overrides.items():
        segment = by_name.get(name)
        if segment is None:
            raise KeyError(f"segment override for unknown segment {name!r}")
        if segment_matches(segment, identity.traits):
            values.update(overrides)
    fallback = values.get("agent_safe_fallback", False)
    if fallback is True or (isinstance(fallback, str) and fallback.lower() == "true"):
        values = {**BASELINE, "agent_safe_fallback": True}
    return values


def parse_value(text: str) -> FlagValue:
    """CLI helper: "12" -> 12, "true" -> True, anything else stays a string."""
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        return text
