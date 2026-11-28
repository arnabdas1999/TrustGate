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
from dataclasses import dataclass, field
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


@dataclass
class PostWriteCheck:
    """What the agent did on the flags Flagsmith serves the incident's identity after a write."""
    resolved: dict[str, Any]
    runs: int = 0
    passes: int = 0
    violations: list[str] = field(default_factory=list)   # distinct, in order seen
    statuses: list[str | None] = field(default_factory=list)
    defaulted: list[str] = field(default_factory=list)    # served by the default handler
    mismatched: list[str] = field(default_factory=list)   # not the value the repair expects

    @property
    def ok(self) -> bool:
        """Clean only if every flag came from Flagsmith with the expected value and every run
        passed. Zero runs is never ok."""
        return (self.runs > 0 and self.passes == self.runs and not self.violations
                and not self.defaulted and not self.mismatched)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b or str(a) == str(b)


def post_write_check(adapter: SqliteDomain, case_id: str, client: ClientLike, *, runs: int = 3,
                     expect: Mapping[str, Any] | None = None) -> PostWriteCheck:
    """After an approved write: resolve the incident identity's flags through the Flagsmith
    SDK (traits from code, so segment overrides apply) and run the incident case on them, fresh,
    `runs` times. `expect` is the flag values the repair should serve this identity; a
    difference, or a flag served by the default handler, fails the check before any run."""
    identity = adapter.identity(case_id)
    resolved, defaulted = resolve(client, identity)
    check = PostWriteCheck(resolved=dict(resolved.values), defaulted=defaulted)
    check.mismatched = [k for k, v in (expect or {}).items()
                        if not _same(resolved.values.get(k), v)]
    if check.defaulted or check.mismatched:
        return check
    for _ in range(runs):
        db = adapter.reset_state(case_id)
        trace = adapter.run_agent(case_id, resolved, identity, db)
        grade = adapter.grade(trace, db)
        check.runs += 1
        check.passes += int(not grade.violations and grade.success)
        check.statuses.append(trace.reported_status)
        check.violations += [v for v in grade.violations if v not in check.violations]
    return check


def main(argv: list[str] | None = None,
         client_factory: Callable[[str], ClientLike] = make_client) -> int:
    from sandbox import get_adapter

    p = argparse.ArgumentParser(description="Run a demo agent once on live Flagsmith flags.")
    p.add_argument("--domain", default="opspilot")
    p.add_argument("--case", default="hero")
    p.add_argument("--baseline-snapshot")
    p.add_argument("--release-snapshot")
    p.add_argument("--post-write", type=int, metavar="RUNS", default=0,
                   help="after a write: run the case RUNS times on the served flags; "
                        "exit 1 unless all pass")
    p.add_argument("--key-var", help="env var holding the ser. key (default: the domain's Prod)")
    p.add_argument("--name", help="incident name, e.g. the bench case opspilot-eval-1")
    args = p.parse_args(argv)
    adapter = get_adapter(args.domain)
    key_var = args.key_var or SERVER_KEY_VARS[adapter.domain]
    key = os.environ.get(key_var, "")
    if not key:
        print(f"{key_var} is not set", file=sys.stderr)
        return 2
    if args.post_write:
        check = post_write_check(adapter, args.case, client_factory(key), runs=args.post_write)
        print(f"served flags: {check.resolved}")
        if check.defaulted:
            print(f"NOT CHECKED: served by the default handler (Flagsmith unreachable?): "
                  f"{check.defaulted}")
        print(f"post-write: {check.passes}/{check.runs} passed; violations: "
              f"{check.violations or 'none'}; statuses: {check.statuses}")
        return 0 if check.ok else 1
    if not (args.baseline_snapshot and args.release_snapshot):
        p.error("--baseline-snapshot and --release-snapshot are required to capture")
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
