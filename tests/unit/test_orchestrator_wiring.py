import pytest

from api.store import Store
from contracts.models import Domain
from orchestrator.search_port import ScriptedSearch, UnavailableSearch
from orchestrator.wiring import (
    ConfigError,
    configs_from_env,
    flag_source_from_env,
    orchestrator_for,
    run_in_background,
)
from verifier.readback import EdgeFlagSource

ENV = {"GATE_URL": "http://gate:8100", "TRUSTGATE_ENVIRONMENT": "Development",
       "FLAGSMITH_SERVER_KEY_DEVELOPMENT": "ser.dev",
       "TRUSTGATE_SEARCH": "scripted:agent_prompt_version"}


def test_configs_default_to_demo_production():
    cfg = configs_from_env({})
    assert cfg[Domain.OPSPILOT].environment == "Demo Production"
    assert cfg[Domain.OPSPILOT].project == "OpsPilot"
    assert cfg[Domain.OPSPILOT].control_identity.traits == {"env": "staging", "tier": 2}
    assert cfg[Domain.VAULTGUARD].control_identity.traits == {"amount_usd": 500,
                                                              "new_vendor": False}


def test_configs_from_env():
    assert configs_from_env(ENV)[Domain.OPSPILOT].environment == "Development"


def test_flag_source_uses_the_environments_server_key():
    source = flag_source_from_env(ENV)("Development")
    assert isinstance(source, EdgeFlagSource)
    with pytest.raises(ConfigError, match="FLAGSMITH_SERVER_KEY_DEMO_PRODUCTION"):
        flag_source_from_env(ENV)("Demo Production")


def test_one_orchestrator_per_store():
    store = Store()
    a = orchestrator_for(store, ENV)
    assert orchestrator_for(store, ENV) is a
    assert isinstance(a.search, ScriptedSearch)
    assert isinstance(orchestrator_for(Store(), {**ENV, "TRUSTGATE_SEARCH": ""}).search,
                      UnavailableSearch)


def test_background_runs_and_never_raises():
    seen = []

    def work():
        seen.append(1)
        raise RuntimeError("boom")

    run_in_background(work, name="t").join(timeout=5)
    assert seen == [1]


def test_reinvestigate_in_background_calls_the_orchestrator(monkeypatch):
    from orchestrator import wiring

    seen = []

    class Orch:
        def reinvestigate(self, inv_id):
            seen.append(inv_id)

    monkeypatch.setattr(wiring, "orchestrator_for", lambda store: Orch())
    wiring.reinvestigate_in_background(object(), "inv_9").join(timeout=5)
    assert seen == ["inv_9"]


def test_sandbox_search_turns_on_the_segment_rules_check():
    from orchestrator import wiring
    from orchestrator.search_port import sandbox_segments

    stores = [object(), object()]  # kept alive: orchestrators are cached per id(store)
    on = wiring.orchestrator_for(stores[0], {"TRUSTGATE_SEARCH": "sandbox"})
    off = wiring.orchestrator_for(stores[1], {"TRUSTGATE_SEARCH": "scripted:agent_prompt_version"})
    assert on.replay_segments is sandbox_segments and off.replay_segments is None
