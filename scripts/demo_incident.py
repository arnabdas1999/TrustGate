"""Demo helper: one live capture, posted to the API, so a live take is one command plus one MCP
call (investigate_incident with the printed id).

    uv run --env-file .env python -m scripts.demo_incident --domain opspilot --environment bench-1

- The agent reads its flags live from Flagsmith (sandbox.live: SDK, local evaluation), with the
  environment's own server-side key, FLAGSMITH_SERVER_KEY_<ENV> (e.g. _BENCH_1). The key is
  never printed.
- Snapshots: --baseline-snapshot / --release-snapshot, default the newest
  data/releases/<environment>/<time>/{baseline,release}_env.json (written by scripts.release).
- An incident is captured only if the run violated something. Otherwise exit 1 and nothing is
  posted.
- The incident is POSTed to <api>/api/incidents. --start also starts the investigation (plain
  or guided); by default the MCP call does that.
Read-only against Flagsmith: nothing here writes a flag.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

RELEASES_DIR = Path("data/releases")


class DemoError(Exception):
    pass


def server_key_name(environment: str) -> str:
    from orchestrator.wiring import server_key_name as name
    return name(environment)


def latest_snapshots(environment: str, releases_dir: Path = RELEASES_DIR) -> tuple[Path, Path]:
    """The newest release folder for `environment` that has both snapshots."""
    folder = releases_dir / environment
    runs = sorted((p for p in folder.glob("*") if p.is_dir()), reverse=True) \
        if folder.exists() else []
    for run in runs:
        base, rel = run / "baseline_env.json", run / "release_env.json"
        if base.exists() and rel.exists():
            return base, rel
    raise DemoError(f"no release snapshots under {folder} (run scripts.release for "
                    f"{environment!r} first, or pass --baseline-snapshot/--release-snapshot)")


def capture(domain: str, case: str, client: Any, *, baseline: Path, release: Path,
            name: str | None = None) -> tuple[Any, Any, list[str]]:
    """sandbox.live.run_live for one request (imported here: sandbox pulls in the models)."""
    from sandbox import get_adapter
    from sandbox.live import run_live
    return run_live(get_adapter(domain), case, client, baseline_snapshot=str(baseline),
                    release_snapshot=str(release), name=name)


def post_incident(http: httpx.Client, incident: Any) -> None:
    r = http.post("/api/incidents", json=incident.model_dump(mode="json"))
    if r.status_code >= 400:
        raise DemoError(f"POST /api/incidents returned HTTP {r.status_code}: {r.text[:200]}")


def start_investigation(http: httpx.Client, incident_id: str, mode: str) -> str:
    r = http.post("/api/investigations", json={"incident_id": incident_id, "mode": mode})
    if r.status_code >= 400:
        raise DemoError(f"POST /api/investigations returned HTTP {r.status_code}: "
                        f"{r.text[:200]}")
    return r.json()["id"]


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         client_factory: Callable[[str], Any] | None = None,
         capture_fn: Callable[..., tuple[Any, Any, list[str]]] = capture,
         http: httpx.Client | None = None, out=print) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--domain", choices=["opspilot", "vaultguard"], default="opspilot")
    p.add_argument("--environment", required=True, help='e.g. "bench-1" or "OpsPilot Prod"')
    p.add_argument("--case", default="hero")
    p.add_argument("--baseline-snapshot", type=Path)
    p.add_argument("--release-snapshot", type=Path)
    p.add_argument("--name", help="incident id (default <domain>-<case>-<8 hex>)")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--start", choices=["plain", "guided"],
                   help="also start the investigation (default: leave it to the MCP call)")
    args = p.parse_args(argv)
    e = os.environ if env is None else env
    try:
        key_name = server_key_name(args.environment)
        key = e.get(key_name, "").strip()
        if not key:
            raise DemoError(f"{key_name} is not set")
        if args.baseline_snapshot and args.release_snapshot:
            baseline, release = args.baseline_snapshot, args.release_snapshot
        else:
            baseline, release = latest_snapshots(args.environment)
        if client_factory is None:
            from sandbox.live import make_client
            client_factory = make_client
        incident, trace, defaulted = capture_fn(args.domain, args.case, client_factory(key),
                                                baseline=baseline, release=release,
                                                name=args.name)
        if defaulted:
            out(f"WARNING: served by the default handler (Flagsmith unreachable?): {defaulted}")
        out(f"agent: reported {trace.reported_status}; tools "
            f"{[c.name for c in trace.tool_calls]}")
        if incident is None:
            out("no violation, nothing captured or posted")
            return 1
        owns_http = http is None
        http = http or httpx.Client(base_url=args.api, timeout=30)
        try:
            post_incident(http, incident)
            out(f"incident: {incident.id}")
            if args.start:
                out(f"investigation: {start_investigation(http, incident.id, args.start)}")
        finally:
            if owns_http:
                http.close()
        return 0
    except (DemoError, httpx.HTTPError) as exc:
        print(f"demo_incident: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 (e.g. LiveError for a non-ser. key: say it plainly)
        print(f"demo_incident: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
