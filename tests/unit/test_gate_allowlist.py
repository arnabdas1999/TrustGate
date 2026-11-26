import pytest

from gate.allowlist import (
    GATE_NATIVE_READS,
    MCP_READS,
    MCP_WRITES,
    REQUIRED_SCOPE_ARGS,
    ReadKind,
    ToolDenied,
    check_read,
)
from gate.scope import GateScope

SCOPE = GateScope(project_id=56664, project_name="OpsPilot",
                  environments={"Development": 121197, "Demo Production": 121198})

# The smallest valid arguments for each read: just its required scope argument, in scope.
MINIMAL_ARGS = {
    "list_project_features": {"project_pk": 56664},
    "get_feature_flag": {"project_pk": 56664},
    "list_project_segments": {"project_pk": 56664},
    "get_project_segment": {"project_pk": 56664},
    "list_project_environments": {"id": 56664},
    "list_feature_segments": {"environment": 121197},
    "get_environment_feature_versions": {"environment_pk": 121197},
    "get_environment_feature_version_states": {"environment_pk": 121197},
    "get_environment_document": {"environment": "Development"},
    "get_audit_log": {},
}


@pytest.mark.parametrize("tool", sorted(MCP_READS))
def test_allowlisted_mcp_reads_pass(tool):
    assert check_read(tool, MINIMAL_ARGS[tool], SCOPE) is ReadKind.MCP


@pytest.mark.parametrize("tool", sorted(GATE_NATIVE_READS))
def test_gate_native_reads_pass(tool):
    assert check_read(tool, MINIMAL_ARGS[tool], SCOPE) is ReadKind.NATIVE


def test_every_read_has_a_required_scope_rule():
    assert set(REQUIRED_SCOPE_ARGS) == MCP_READS | GATE_NATIVE_READS
    assert REQUIRED_SCOPE_ARGS["get_audit_log"] is None


@pytest.mark.parametrize("tool", sorted(t for t, a in REQUIRED_SCOPE_ARGS.items() if a))
def test_read_without_its_required_scope_arg_is_refused(tool):
    with pytest.raises(ToolDenied, match="requires"):
        check_read(tool, {}, SCOPE)


@pytest.mark.parametrize(("tool", "args"), [
    ("list_project_features", {"project_id": 56664}),        # right project, wrong arg name
    ("list_project_features", {"Project_PK": 56664}),        # names match exactly
    ("list_project_environments", {"project_pk": 56664}),    # this tool takes "id"
    ("list_feature_segments", {"environment_pk": 121197}),   # this tool takes "environment"
    ("get_environment_feature_versions", {"environment": 121197}),
    ("get_environment_document", {"environment_pk": 121197}),
])
def test_scope_arg_under_another_name_does_not_satisfy_the_requirement(tool, args):
    with pytest.raises(ToolDenied, match="requires"):
        check_read(tool, args, SCOPE)


@pytest.mark.parametrize("value", [True, False, None, [56664], {"id": 56664}, 56664.0])
def test_non_scalar_project_values_are_refused(value):
    with pytest.raises(ToolDenied, match="scalar"):
        check_read("list_project_features", {"project_pk": value}, SCOPE)


@pytest.mark.parametrize("value", [True, None, [121197], {"id": 121197}, ("Development",)])
def test_non_scalar_environment_values_are_refused(value):
    with pytest.raises(ToolDenied, match="scalar"):
        check_read("list_feature_segments", {"environment": value}, SCOPE)


def test_non_scalar_optional_scope_value_is_refused():
    with pytest.raises(ToolDenied, match="scalar"):
        check_read("get_audit_log", {"environment": ["Development", "Production"]}, SCOPE)


def test_non_scope_args_may_be_lists():
    args = {"environment": "Development", "features": ["agent_prompt_version"]}
    assert check_read("get_audit_log", args, SCOPE) is ReadKind.NATIVE


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
    assert check_read("list_project_features", {"project_pk": 56664}, SCOPE) is ReadKind.MCP
    assert check_read("list_project_features", {"project_pk": "OpsPilot"}, SCOPE) is ReadKind.MCP
    with pytest.raises(ToolDenied, match="project"):
        check_read("list_project_features", {"project_pk": 1}, SCOPE)
    with pytest.raises(ToolDenied, match="project"):
        check_read("list_project_features", {"project_pk": 56664, "project_id": 1}, SCOPE)


@pytest.mark.parametrize("args", [
    {"environment": 121197, "feature": 306127},
    {"environment": "121198", "feature": 306127},
    {"environment": "Demo Production", "feature": 306127},
])
def test_read_in_allowed_environment(args):
    assert check_read("list_feature_segments", args, SCOPE) is ReadKind.MCP


@pytest.mark.parametrize("args", [
    {"environment": 999, "feature": 306127},
    {"environment_pk": 121201, "feature_pk": 306127},
    {"environment_api_key": "someone-elses-key"},
    {"environment_pk": 121197, "environment_api_key": "someone-elses-key"},
])
def test_read_in_other_environment_is_refused(args):
    with pytest.raises(ToolDenied, match="environment"):
        check_read("get_environment_feature_versions", args, SCOPE)


def test_list_project_environments_id_is_our_project():
    assert check_read("list_project_environments", {"id": 56664}, SCOPE) is ReadKind.MCP
    with pytest.raises(ToolDenied, match="project"):
        check_read("list_project_environments", {"id": 1}, SCOPE)


def test_project_pk_is_checked():
    with pytest.raises(ToolDenied, match="project"):
        check_read("list_project_segments", {"project_pk": 2}, SCOPE)


def test_version_uuid_is_not_mistaken_for_an_environment():
    args = {"environment_pk": 121197, "feature_pk": 306127,
            "environment_feature_version_pk": "e859594d-6933-415e-9cd5-bd0fa7384547"}
    assert check_read("get_environment_feature_version_states", args, SCOPE) is ReadKind.MCP


def test_unknown_scope_like_arguments_are_refused():
    with pytest.raises(ToolDenied, match="argument"):
        check_read("list_project_features", {"project_pk": 56664, "projects": [1, 2]}, SCOPE)


def test_organisation_arguments_are_refused():
    with pytest.raises(ToolDenied, match="organisation"):
        check_read("list_project_features", {"organisation": 1}, SCOPE)
    with pytest.raises(ToolDenied, match="organisation"):
        check_read("list_project_features", {"organization_id": 1}, SCOPE)


def test_non_dict_args_are_refused():
    with pytest.raises(ToolDenied):
        check_read("list_project_features", ["project_id", 56664], SCOPE)  # type: ignore[arg-type]
