"""Live mode: the demo agents read their flags from Flagsmith per request, like a real product.

- Flagsmith Python SDK, local evaluation, 15 s refresh, server-side key (ser.) only.
- default_flag_handler serves the safe baseline if Flagsmith is unreachable; those values are
  marked so a capture never pretends they came from Flagsmith.
- Identity and traits come from code (the case's cluster or wire), never from user input.
- Every live run captures an Incident with the resolved flags the agent actually received.

Replays never come through here: search uses captured config plus overrides.

    uv run --env-file .env python -m sandbox.live --case hero \
        --baseline-snapshot data/releases/.../baseline_env.json \
        --release-snapshot data/releases/.../release_env.json
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from contracts.models import ConfigSource, Domain, FlagConfig, Identity, Incident, Trace
from sandbox.adapter import SqliteDomain
from sandbox.flags import BASELINE

FLAGS = list(BASELINE)
INCIDENTS_DIR = Path("bench/incidents")
SERVER_KEY_VARS = {Domain.OPSPILOT: "FLAGSMITH_SERVER_KEY_OPSPILOT_PROD",
                   Domain.VAULTGUARD: "FLAGSMITH_SERVER_KEY_VAULTGUARD_PROD"}


class LiveError(Exception):
    pass


class FlagsLike(Protocol):
    def get_flag(self, name: str) -> Any: ...


class ClientLike(Protocol):
    def get_identity_flags(self, identifier: str, traits: Mapping[str, Any]) -> FlagsLike: ...


def make_client(server_key: str) -> ClientLike:
    if not server_key.startswith("ser."):
        raise LiveError("live mode needs a server-side environment key (ser.)")
    from flagsmith import Flagsmith
    from flagsmith.models import DefaultFlag

    def safe_default(name: str) -> DefaultFlag:
        return DefaultFlag(enabled=True, value=BASELINE.get(name))

    return Flagsmith(environment_key=server_key, enable_local_evaluation=True,
                     environment_refresh_interval_seconds=15, default_flag_handler=safe_default)


def resolve(client: ClientLike, identity: Identity) -> tuple[FlagConfig, list[str]]:
    """The flags this identity is served now, and which of them came from the default handler."""
    flags = client.get_identity_flags(identity.identifier, traits=dict(identity.traits))
    values: dict[str, Any] = {}
    defaulted: list[str] = []
    for name in FLAGS:
        flag = flags.get_flag(name)
        if getattr(flag, "is_default", False):
            defaulted.append(name)
        values[name] = BASELINE[name] if flag.value is None else flag.value
    return FlagConfig(values=values, source=ConfigSource.CAPTURED), defaulted


def snapshot_ref(path: str) -> str:
    """The orchestrator's reference form: "file:<path>" (gate: refs pass through)."""
    return path if path.startswith(("file:", "gate:")) else f"file:{path}"


def capture(adapter: SqliteDomain, case_id: str, identity: Identity, resolved: FlagConfig,
            trace: Trace, db_path: Path, *, baseline_snapshot: str, release_snapshot: str,
            out_dir: Path = INCIDENTS_DIR, name: str | None = None) -> Incident:
    """Writes <out_dir>/<name>.json (default name: <domain>-<case>-<8 hex>). Pass the bench
    case name (e.g. opspilot-eval-1) so bench/run_arms.py finds it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    incident_id = name or f"{adapter.domain}-{case_id}-{uuid.uuid4().hex[:8]}"
    db_copy = out_dir / f"{incident_id}.db"
    shutil.copyfile(db_path, db_copy)
    incident = Incident(id=incident_id, domain=adapter.domain, case_id=case_id,
                        identity=identity, resolved_flags=resolved,
                        baseline_snapshot=snapshot_ref(baseline_snapshot),
                        release_snapshot=snapshot_ref(release_snapshot),
                        db_snapshot=str(db_copy), trace=trace, created_at=datetime.now(UTC))
    (out_dir / f"{incident_id}.json").write_text(incident.model_dump_json(indent=2),
                                                 encoding="utf-8")
    return incident


def run_live(adapter: SqliteDomain, case_id: str, client: ClientLike, **capture_kwargs: Any
             ) -> tuple[Incident | None, Trace, list[str]]:
    """One live request. Captures an incident only when the run violated something."""
    identity = adapter.identity(case_id)
    resolved, defaulted = resolve(client, identity)
    db = adapter.reset_state(case_id)
    trace = adapter.run_agent(case_id, resolved, identity, db)
    grade = adapter.grade(trace, db)
    incident = capture(adapter, case_id, identity, resolved, trace, db, **capture_kwargs) \
        if grade.violations else None
    return incident, trace, defaulted


def main(argv: list[str] | None = None,
         client_factory: Callable[[str], ClientLike] = make_client) -> int:
    from sandbox import get_adapter

    p = argparse.ArgumentParser(description="Run a demo agent once on live Flagsmith flags.")
    p.add_argument("--domain", default="opspilot")
    p.add_argument("--case", default="hero")
    p.add_argument("--baseline-snapshot", required=True)
    p.add_argument("--release-snapshot", required=True)
    p.add_argument("--name", help="incident name, e.g. the bench case opspilot-eval-1")
    args = p.parse_args(argv)
    adapter = get_adapter(args.domain)
    key = os.environ.get(SERVER_KEY_VARS[adapter.domain], "")
    if not key:
        print(f"{SERVER_KEY_VARS[adapter.domain]} is not set", file=sys.stderr)
        return 2
    incident, trace, defaulted = run_live(adapter, args.case, client_factory(key),
                                          baseline_snapshot=args.baseline_snapshot,
                                          release_snapshot=args.release_snapshot,
                                          name=args.name)
    print(f"resolved flags: {trace.flags.values}")
    if defaulted:
        print(f"WARNING: served by the default handler (Flagsmith unreachable?): {defaulted}")
    print(f"reported status: {trace.reported_status}; tools: {[c.name for c in trace.tool_calls]}")
    print(f"incident captured: {incident.id}" if incident else "no violation, nothing captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
