"""Read allowlist for the investigator. Default deny.

Only the names below can be read through POST /gate/read: explicit MCP tool names (never a
"list_*" or "get_*" pattern) plus two gate-native reads served by the Flagsmith CLI or SDK.
Anything else is refused, including read-only tools we chose not to expose (organisation
listings, code references) and every tool Flagsmith adds later. Writes never go through
here: they go through /gate/write with a signed, single-use approval.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from gate.scope import GateScope

MCP_READS: frozenset[str] = frozenset({
    "list_project_features",
    "get_feature_flag",
    "list_project_segments",
    "get_project_segment",
    "list_feature_segments",
    "list_project_environments",
    # v2 versioning: who/when per feature, since the audit log is empty on our plan
    "get_environment_feature_versions",
    "get_environment_feature_version_states",
})

GATE_NATIVE_READS: frozenset[str] = frozenset({"get_audit_log", "get_environment_document"})

# The only write the executor sends (v2 environments). Listed so it can never be a read.
MCP_WRITES: frozenset[str] = frozenset({"create_environment_feature_version"})


class ReadKind(StrEnum):
    MCP = "mcp"
    NATIVE = "native"


class ToolDenied(Exception):
    """The investigator asked for something the gate does not allow."""


def check_read(tool: str, args: dict[str, Any], scope: GateScope) -> ReadKind:
    """Return how to serve an allowlisted read, or raise ToolDenied.

    Names must match exactly (no case folding or trimming). Arguments that name a project or
    environment must point inside the gate's scope; organisation arguments are refused.
    """
    if not isinstance(tool, str) or tool in MCP_WRITES:
        raise ToolDenied(f"{tool!r} is not on the read allowlist")
    if tool in MCP_READS:
        kind = ReadKind.MCP
    elif tool in GATE_NATIVE_READS:
        kind = ReadKind.NATIVE
    else:
        raise ToolDenied(f"{tool!r} is not on the read allowlist")

    if not isinstance(args, dict):
        raise ToolDenied("read arguments must be an object")
    for key, value in args.items():
        k = key.lower()
        if "organisation" in k or "organization" in k:
            raise ToolDenied(f"{tool}: organisation arguments are not allowed ({key})")
        if "project" in k and not scope.allows_project(value):
            raise ToolDenied(f"{tool}: project {value!r} is outside the gate's scope")
        if "environment" in k and not scope.allows_environment(value):
            raise ToolDenied(f"{tool}: environment {value!r} is outside the gate's scope")
    return kind
