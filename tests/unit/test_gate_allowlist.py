import pytest

from gate.allowlist import (
    GATE_NATIVE_READS,
    MCP_READS,
    MCP_WRITES,
    ReadKind,
    ToolDenied,
    check_read,
)
from gate.scope import GateScope

SCOPE = GateScope(project_id=56664, project_name="OpsPilot",
                  environments={"Development": 121197, "Demo Production": 121198})


@pytest.mark.parametrize("tool", sorted(MCP_READS))
def test_allowlisted_mcp_reads_pass(tool):
    assert check_read(tool, {}, SCOPE) is ReadKind.MCP


@pytest.mark.parametrize("tool", sorted(GATE_NATIVE_READS))
def test_gate_native_reads_pass(tool):
    assert check_read(tool, {}, SCOPE) is ReadKind.NATIVE


def test_allowlist_is_exactly_the_planned_names():
    assert MCP_READS == {
        "list_project_features", "get_feature_flag", "list_project_segments",
        "get_project_segment", "list_feature_segments", "list_project_environments",
        "get_environment_feature_versions", "get_environment_feature_version_states",
    }
    assert GATE_NATIVE_READS == {"get_audit_log", "get_environment_document"}


@pytest.mark.parametrize("tool", [
    "list_organizations", "list_organization_groups", "list_organization_invites",
    "get_feature_code_references",
])
def test_reads_denied_by_name_are_refused(tool):
    with pytest.raises(ToolDenied, match="not on the read allowlist"):
        check_read(tool, {}, SCOPE)


@pytest.mark.parametrize("tool", [
    "list_projects_v2", "get_everything", "", "List_Project_Features",
    " list_project_features", "list_project_features ", "list_project_features\n",
])
def test_unknown_and_lookalike_tools_are_refused(tool):
    with pytest.raises(ToolDenied):
        check_read(tool, {}, SCOPE)


@pytest.mark.parametrize("tool", sorted(MCP_WRITES | {
    "create_segment_override", "update_feature_state", "delete_feature_segment",
    "update_environment_feature_state", "create_environment_feature_version_state",
    "update_environment_feature_version_state", "publish_environment_feature_version",
    "create_feature", "delete_feature", "create_project_segment",
}))
def test_write_tools_are_never_reads(tool):
    with pytest.raises(ToolDenied):
        check_read(tool, {}, SCOPE)


def test_reads_and_writes_do_not_overlap():
    assert not (MCP_READS | GATE_NATIVE_READS) & MCP_WRITES


def test_read_scoped_to_our_project():
    assert check_read("list_project_features", {"project_id": 56664}, SCOPE) is ReadKind.MCP
    with pytest.raises(ToolDenied, match="project"):
        check_read("list_project_features", {"project_id": 1}, SCOPE)


@pytest.mark.parametrize("args", [
    {"environment": 121197, "feature": 306127},
    {"environment_pk": "121198", "feature_pk": 306127},
])
def test_read_in_allowed_environment(args):
    assert check_read("list_feature_segments", args, SCOPE) is ReadKind.MCP


@pytest.mark.parametrize("args", [
    {"environment": 999, "feature": 306127},
    {"environment_pk": 121201, "feature_pk": 306127},
    {"environment_api_key": "someone-elses-key"},
])
def test_read_in_other_environment_is_refused(args):
    with pytest.raises(ToolDenied, match="environment"):
        check_read("get_environment_feature_versions", args, SCOPE)


def test_organisation_arguments_are_refused():
    with pytest.raises(ToolDenied, match="organisation"):
        check_read("list_project_features", {"organisation": 1}, SCOPE)
    with pytest.raises(ToolDenied, match="organisation"):
        check_read("list_project_features", {"organization_id": 1}, SCOPE)


def test_non_dict_args_are_refused():
    with pytest.raises(ToolDenied):
        check_read("list_project_features", ["project_id", 56664], SCOPE)  # type: ignore[arg-type]
