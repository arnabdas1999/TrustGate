"""Read allowlist for the investigator. Default deny.

Only the names below can be read through POST /gate/read: explicit MCP tool names (never a
"list_*" or "get_*" pattern) plus two reads the gate serves itself (the edge environment
document over HTTP, and its own audit trail).
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


# Argument names (from the MCP tool schemas, Nov 26) that select a project or environment.
PROJECT_ARGS: frozenset[str] = frozenset({"project", "project_id", "project_pk"})
ENVIRONMENT_ARGS: frozenset[str] = frozenset({
    "environment", "environment_id", "environment_pk", "environment_api_key"})
# Tools whose bare "id" argument is the project id.
PROJECT_ID_IS_ID: frozenset[str] = frozenset({"list_project_environments"})
# Contain "environment" but are not environments: a version uuid inside an allowed environment.
NON_SCOPE_ARGS: frozenset[str] = frozenset({"environment_feature_version_pk"})

# The scope argument each read must carry (exact name, from the live MCP schemas on Nov 26), so
# every read is pinned to our project or one of our environments. None: no scope argument.
REQUIRED_SCOPE_ARGS: dict[str, str | None] = {
    "list_project_features": "project_pk",
    "get_feature_flag": "project_pk",
    "list_project_segments": "project_pk",
    "get_project_segment": "project_pk",
    "list_project_environments": "id",  # the project id
    "list_feature_segments": "environment",
    "get_environment_feature_versions": "environment_pk",
    "get_environment_feature_version_states": "environment_pk",
    "get_environment_document": "environment",
    "get_audit_log": None,
}


class ReadKind(StrEnum):
    MCP = "mcp"
    NATIVE = "native"


class ToolDenied(Exception):
    """The investigator asked for something the gate does not allow."""


def check_read(tool: str, args: dict[str, Any], scope: GateScope) -> ReadKind:
    """Return how to serve an allowlisted read, or raise ToolDenied.

    Names must match exactly (no case folding or trimming). Each read must carry its required
    scope argument. Arguments that name a project or environment must be a single int or str
    pointing inside the gate's scope; organisation arguments are refused.
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
    project_args = PROJECT_ARGS | ({"id"} if tool in PROJECT_ID_IS_ID else set())
    for key, value in args.items():
        k = key.lower()
        if "organisation" in k or "organization" in k:
            raise ToolDenied(f"{tool}: organisation arguments are not allowed ({key})")
        if (k in project_args or k in ENVIRONMENT_ARGS) and not _is_scalar(value):
            raise ToolDenied(f"{tool}: scope argument {key!r} must be a scalar id or name")
        if k in project_args:
            if not scope.allows_project(value):
                raise ToolDenied(f"{tool}: project {value!r} is outside the gate's scope")
        elif k in ENVIRONMENT_ARGS:
            if not scope.allows_environment(value):
                raise ToolDenied(f"{tool}: environment {value!r} is outside the gate's scope")
        elif k not in NON_SCOPE_ARGS and ("project" in k or "environment" in k):
            # A scope-like name we don't know how to check: refuse rather than guess.
            raise ToolDenied(f"{tool}: unexpected scope argument {key!r}")
    required = REQUIRED_SCOPE_ARGS[tool]
    if required is not None and required not in args:
        raise ToolDenied(f"{tool} requires the scope argument {required!r}")
    return kind


def _is_scalar(value: object) -> bool:
    # bool is an int subclass, but True is never a project or environment id.
    return isinstance(value, int | str) and not isinstance(value, bool)
