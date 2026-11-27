import pytest

from gate.scope import GateScope

SCOPE = GateScope(project_id=56664, project_name="OpsPilot",
                  environments={"Development": 121197, "OpsPilot Prod": 121198})


@pytest.mark.parametrize("project", [56664, "56664", "OpsPilot"])
def test_our_project_by_id_or_name_is_allowed(project):
    assert SCOPE.allows_project(project)


@pytest.mark.parametrize("project", [1, "1", "opspilot", " OpsPilot", "", " ", None])
def test_other_projects_are_refused(project):
    assert not SCOPE.allows_project(project)


@pytest.mark.parametrize("name", ["", " ", "\t\n"])
def test_empty_or_blank_project_name_matches_nothing(name):
    scope = GateScope(project_id=56664, project_name=name)
    assert not scope.allows_project("")
    assert not scope.allows_project(name)
    assert scope.allows_project(56664)


@pytest.mark.parametrize("environment", [121197, "121198", "Development", "OpsPilot Prod"])
def test_our_environments_by_id_or_name_are_allowed(environment):
    assert SCOPE.allows_environment(environment)


@pytest.mark.parametrize("environment", [999, "Production", "development", ""])
def test_other_environments_are_refused(environment):
    assert not SCOPE.allows_environment(environment)
