"""Builds the orchestrator from the API process's environment and runs it off the request.

Investigations (search) and verification (polling up to 60 s) take far longer than an HTTP
request, so the API starts them in daemon threads and the UI polls the investigation.

Environment (API container, .env):
- GATE_URL (default http://localhost:8100)
- TRUSTGATE_ENVIRONMENT: the environment the repair is written to (default Demo Production)
- TRUSTGATE_PROJECT_OPSPILOT / TRUSTGATE_PROJECT_VAULTGUARD: project names (default the domain)
- FLAGSMITH_SERVER_KEY_<ENV>: a ser. key per environment, for the verifier's read-back
- TRUSTGATE_SEARCH: "scripted:<flag>,..." for the plumbing demo until search/ lands
- LLM_PROVIDER=gemini, LLM_API_KEY, MODEL_CHEAP (investigator's model), LLM_LIVE=0 to switch
  the LLM off (diff order and template explanations)
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any

from agent.llm import llm_from_env
from contracts.models import Domain, Identity
from orchestrator.gate_client import GateClient
from orchestrator.lifecycle import DomainConfig, Orchestrator
from orchestrator.repo import DictRepo
from orchestrator.search_port import search_from_env
from verifier.readback import EdgeFlagSource, FlagSource

log = logging.getLogger("trustgate.orchestrator")

# Identities outside each domain's repair segment, used to prove a repair stayed narrow.
CONTROL_IDENTITIES = {
    Domain.OPSPILOT: Identity(identifier="staging-api", traits={"env": "staging", "tier": 2}),
    Domain.VAULTGUARD: Identity(identifier="small-known-vendor-wire",
                                traits={"amount_usd": 500, "new_vendor": False}),
}


class ConfigError(Exception):
    pass


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def server_key_name(environment: str) -> str:
    return "FLAGSMITH_SERVER_KEY_" + re.sub(r"[^A-Z0-9]+", "_", environment.upper()).strip("_")


def configs_from_env(env: Mapping[str, str] | None = None) -> dict[Domain, DomainConfig]:
    e = _env(env)
    environment = e.get("TRUSTGATE_ENVIRONMENT", "Demo Production")
    return {
        Domain.OPSPILOT: DomainConfig(e.get("TRUSTGATE_PROJECT_OPSPILOT", "OpsPilot"),
                                      environment, CONTROL_IDENTITIES[Domain.OPSPILOT]),
        Domain.VAULTGUARD: DomainConfig(e.get("TRUSTGATE_PROJECT_VAULTGUARD", "VaultGuard"),
                                        environment, CONTROL_IDENTITIES[Domain.VAULTGUARD]),
    }


def flag_source_from_env(env: Mapping[str, str] | None = None) -> Callable[[str], FlagSource]:
    e = _env(env)

    def source(environment: str) -> FlagSource:
        key = e.get(server_key_name(environment), "")
        if not key:
            raise ConfigError(f"{server_key_name(environment)} is not set")
        return EdgeFlagSource(key)

    return source


_orchestrators: dict[int, Orchestrator] = {}
_lock = threading.Lock()


def orchestrator_for(store: Any, env: Mapping[str, str] | None = None) -> Orchestrator:
    """One orchestrator per API store, sharing its in-memory objects."""
    with _lock:
        if id(store) not in _orchestrators:
            e = _env(env)
            _orchestrators[id(store)] = Orchestrator(
                repo=DictRepo(store), gate=GateClient(e.get("GATE_URL", "http://localhost:8100")),
                search=search_from_env(e), configs=configs_from_env(e),
                flag_source=flag_source_from_env(e), llm=llm_from_env(e),
                llm_model=e.get("MODEL_CHEAP", ""))
        return _orchestrators[id(store)]


def run_in_background(work: Callable[[], Any], *, name: str) -> threading.Thread:
    def guarded() -> None:
        try:
            work()
        except Exception:
            log.exception("%s failed", name)

    thread = threading.Thread(target=guarded, name=name, daemon=True)
    thread.start()
    return thread


def start_in_background(store: Any, investigation_id: str) -> threading.Thread:
    orch = orchestrator_for(store)
    return run_in_background(lambda: orch.run(investigation_id),
                             name=f"investigate-{investigation_id}")


def verify_in_background(store: Any, investigation_id: str) -> threading.Thread:
    orch = orchestrator_for(store)
    return run_in_background(lambda: orch.verify_after_write(investigation_id),
                             name=f"verify-{investigation_id}")


def reinvestigate_in_background(store: Any, investigation_id: str) -> threading.Thread:
    """For the API's 409 (STALE) branch: start over from the snapshots."""
    orch = orchestrator_for(store)
    return run_in_background(lambda: orch.reinvestigate(investigation_id),
                             name=f"reinvestigate-{investigation_id}")
