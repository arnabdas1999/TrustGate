"""Turns approved WriteOps into the exact Flagsmith MCP calls, and sends only those.

All our environments use v2 feature versioning, so every write is one
`create_environment_feature_version` call per feature, published immediately:
- create_segment_override -> feature_states_to_create (segment override)
- update_feature_state    -> feature_states_to_update (environment default)
- delete_feature_segment  -> segment_ids_to_delete_overrides
The WriteOp tool names are logical operations; this module is the only place they become calls.

`render` is pure and deterministic (same ops and ids give the same calls), so the UI can show
"the exact call" before approval and the executor sends exactly that. `send` refuses any call
that is not a v2 write rendered here, and never retries.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from contracts.models import FlagValue, WriteOp
from gate.scope import GateScope

V2_WRITE_TOOL = "create_environment_feature_version"
INT_MIN, INT_MAX = -(2**31), 2**31 - 1


class WriterError(Exception):
    pass


@dataclass(frozen=True)
class McpCall:
    tool: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments}


class IdResolver(Protocol):
    def feature_id(self, name: str) -> int: ...
    def segment_id(self, name: str) -> int: ...


@dataclass
class StaticIds:
    features: dict[str, int] = field(default_factory=dict)
    segments: dict[str, int] = field(default_factory=dict)

    def feature_id(self, name: str) -> int:
        if name not in self.features:
            raise WriterError(f"unknown feature {name!r}")
        return self.features[name]

    def segment_id(self, name: str) -> int:
        if name not in self.segments:
            raise WriterError(f"unknown segment {name!r}")
        return self.segments[name]


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("results", payload.get("result", []))
    return payload if isinstance(payload, list) else []


class McpIdResolver:
    """Resolves names to ids with allowlisted MCP reads on our project, cached per gate run."""

    def __init__(self, mcp: Any, *, project_id: int):
        self._mcp = mcp
        self._project_id = project_id
        self._ids: StaticIds | None = None
        self._segments_loaded = False

    def _load(self, tool: str) -> dict[str, int]:
        result = self._mcp.call_tool(tool, {"project_pk": self._project_id, "page_size": 100},
                                     is_write=False)
        if result.is_error:
            raise WriterError(f"{tool} failed: {result.text()[:200]}")
        return {row["name"]: row["id"] for row in _rows(result.json())}

    def feature_id(self, name: str) -> int:
        if self._ids is None:
            self._ids = StaticIds(features=self._load("list_project_features"))
        return self._ids.feature_id(name)

    def segment_id(self, name: str) -> int:
        if self._ids is None:
            self._ids = StaticIds(features=self._load("list_project_features"))
        if not self._segments_loaded:
            self._ids.segments = self._load("list_project_segments")
            self._segments_loaded = True
        return self._ids.segment_id(name)


def _typed_value(value: FlagValue | None, op: WriteOp) -> dict[str, Any]:
    if value is None:
        raise WriterError(f"{op.tool} on {op.feature} needs a value")
    if isinstance(value, bool):
        return {"type": "bool", "boolean_value": value}
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise WriterError(f"{op.feature}: {value} does not fit a Flagsmith integer")
        return {"type": "int", "integer_value": value}
    # Flagsmith has no float type: floats travel as strings ("0.0"), like the dashboard does.
    return {"type": "unicode", "string_value": str(value)}


def render(ops: Sequence[WriteOp], scope: GateScope, ids: IdResolver, *, proposal_id: str,
           approval_id: str) -> list[McpCall]:
    if not ops:
        raise WriterError("no ops to render")
    environments = {op.environment for op in ops}
    if len(environments) != 1:
        raise WriterError(f"one approval writes one environment, got {sorted(environments)}")
    (environment,) = environments
    if environment not in scope.environments:
        raise WriterError(f"environment {environment!r} is outside the gate's scope")

    by_feature: dict[str, list[WriteOp]] = defaultdict(list)
    for op in ops:
        by_feature[op.feature].append(op)

    calls = []
    for feature in sorted(by_feature):
        fid = ids.feature_id(feature)
        creates: dict[int, dict[str, Any]] = {}
        deletes: set[int] = set()
        updates: list[dict[str, Any]] = []
        for op in by_feature[feature]:
            enabled = True if op.enabled is None else op.enabled
            if op.tool == "create_segment_override":
                sid = ids.segment_id(op.segment or "")
                if sid in creates or sid in deletes:
                    raise WriterError(f"segment {op.segment!r} appears twice for {feature}")
                creates[sid] = {"feature": fid, "enabled": enabled,
                                "feature_segment": {"segment": sid},
                                "feature_state_value": _typed_value(op.value, op)}
            elif op.tool == "delete_feature_segment":
                sid = ids.segment_id(op.segment or "")
                if sid in creates or sid in deletes:
                    raise WriterError(f"segment {op.segment!r} appears twice for {feature}")
                deletes.add(sid)
            else:  # update_feature_state: the environment default
                if updates:
                    raise WriterError(f"two default-value changes for {feature}")
                updates.append({"feature": fid, "enabled": enabled, "feature_segment": None,
                                "feature_state_value": _typed_value(op.value, op)})
        arguments: dict[str, Any] = {
            "environment_pk": scope.environments[environment],
            "feature_pk": fid,
            "publish_immediately": True,
            "description": f"trustgate proposal={proposal_id} approval={approval_id}",
        }
        if creates:
            arguments["feature_states_to_create"] = [creates[s] for s in sorted(creates)]
        if updates:
            arguments["feature_states_to_update"] = updates
        if deletes:
            arguments["segment_ids_to_delete_overrides"] = sorted(deletes)
        calls.append(McpCall(V2_WRITE_TOOL, arguments))
    return calls


@dataclass
class WriteResult:
    call: McpCall
    ok: bool
    detail: str = ""
    version_uuid: str | None = None


def send(calls: Sequence[McpCall], mcp: Any) -> list[WriteResult]:
    """Send rendered calls in order, each once. Stops at the first failure."""
    for call in calls:
        if call.tool != V2_WRITE_TOOL or "publish_immediately" not in call.arguments:
            raise WriterError(f"refusing to send a call the renderer did not produce: {call.tool}")
    results = []
    for call in calls:
        try:
            result = mcp.call_tool(call.tool, call.arguments, is_write=True)
        except Exception as exc:  # noqa: BLE001 (network error on a write: report, never retry)
            results.append(WriteResult(call, ok=False, detail=f"{type(exc).__name__}: {exc}"))
            break
        if result.is_error:
            results.append(WriteResult(call, ok=False, detail=result.text()[:500]))
            break
        try:
            body = result.json()
        except ValueError:
            body = {}
        uuid = body.get("uuid") if isinstance(body, dict) else None
        results.append(WriteResult(call, ok=True, version_uuid=uuid))
    return results
