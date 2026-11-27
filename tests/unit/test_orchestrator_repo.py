from datetime import UTC, datetime

import pytest

from api.store import Store
from contracts.models import (
    Domain,
    Investigation,
    InvestigationStatus,
    Timestamps,
)
from orchestrator.repo import DictRepo, SqliteRepo

NOW = datetime(2026, 11, 26, 19, 0, tzinfo=UTC)


def _inv(status=InvestigationStatus.CAPTURING):
    return Investigation(id="inv_1", incident_id="inc_1", domain=Domain.OPSPILOT, status=status,
                         timestamps=Timestamps(alert=NOW))


@pytest.fixture(params=["sqlite", "dict"])
def repo(request, tmp_path):
    if request.param == "sqlite":
        return SqliteRepo(tmp_path / "orch" / "trustgate.db")
    return DictRepo(Store())


def test_investigation_round_trip(repo):
    repo.put_investigation(_inv())
    got = repo.get_investigation("inv_1")
    assert got.status is InvestigationStatus.CAPTURING
    got.status = InvestigationStatus.SEARCHING
    repo.put_investigation(got)
    assert repo.get_investigation("inv_1").status is InvestigationStatus.SEARCHING


def test_missing_is_none(repo):
    assert repo.get_investigation("nope") is None
    assert repo.get_proposal("nope") is None
    assert repo.get_incident("nope") is None
    assert repo.get_calls("nope") == []


def test_exact_calls_are_kept_per_proposal(repo):
    calls = [{"tool": "create_environment_feature_version", "arguments": {"feature_pk": 1}}]
    repo.put_calls("prop_1", calls)
    assert repo.get_calls("prop_1") == calls


def test_sqlite_survives_reopen(tmp_path):
    SqliteRepo(tmp_path / "t.db").put_investigation(_inv())
    assert SqliteRepo(tmp_path / "t.db").get_investigation("inv_1").id == "inv_1"


def test_dict_repo_writes_into_the_api_store():
    store = Store()
    DictRepo(store).put_investigation(_inv())
    assert store.investigations["inv_1"].incident_id == "inc_1"


def test_served_values_round_trip_in_both_repos(tmp_path):
    from types import SimpleNamespace

    from orchestrator.repo import DictRepo, SqliteRepo
    for repo in (SqliteRepo(tmp_path / "r.db"),
                 DictRepo(SimpleNamespace(incidents={}, investigations={}, proposals={}))):
        assert repo.get_served("prop_1") is None
        repo.put_served("prop_1", {"agent_max_steps": 6, "agent_prompt_version": "v1_strict"})
        assert repo.get_served("prop_1") == {"agent_max_steps": 6,
                                             "agent_prompt_version": "v1_strict"}
