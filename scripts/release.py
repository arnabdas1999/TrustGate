"""Simulate the team shipping a release: snapshot, apply the 5-flag release, snapshot again.

This is NOT TrustGate's write path. It plays the role of a developer changing flags in
Flagsmith, through the normal Admin API with the organisation key, so the demo has a real
release to investigate. TrustGate later finds the culprit by diffing the two snapshots.

    uv run python -m scripts.release --environment Development
    uv run python -m scripts.release --environment Development \
        --revert data/releases/Development/<time>/baseline_env.json
    uv run python -m scripts.release --environment Development --dry-run

Keys come from the environment only: FLAGSMITH_API_KEY (organisation key) and
FLAGSMITH_SERVER_KEY (the ser. key of the SAME environment, for the edge document).
FLAGSMITH_PROJECT_ID (or --project-id) picks the project. Any environment other than
Development needs --confirm-environment repeating its exact name.

Snapshots go to data/releases/<environment>/<UTC time>/ (git-ignored):
- baseline_env.json, then release_env.json
- each with the edge document (current right after a write; it's what the SDK evaluates),
  the admin document (it can lag a write by about a minute, so it's kept for reference), and
  the flag values taken from the edge
- release.json, the changes applied
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from contracts.models import FlagChange, FlagValue

API_URL = "https://api.flagsmith.com"
EDGE_URL = "https://edge.api.flagsmith.com"
SAFE_ENVIRONMENT = "Development"

# Frozen plan p.5: the release changes 5 flags at once; agent_temperature and
# agent_safe_fallback stay as they are.
PLAN_RELEASE: dict[str, FlagValue] = {
    "agent_model": "model_cheap",
    "agent_prompt_version": "v2_fast",
    "agent_tool_policy": "expanded",
    "agent_max_steps": 12,
    "agent_context_mode": "full_history",
}


class ReleaseError(Exception):
    pass


def check_environment_allowed(environment: str, confirm: str | None) -> None:
    if environment != SAFE_ENVIRONMENT and confirm != environment:
        raise ReleaseError(f"{environment!r} is not {SAFE_ENVIRONMENT}: pass "
                           f"--confirm-environment {environment!r} (needs Naman's OK)")


def typed_value(value: FlagValue) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "bool", "boolean_value": value}
    if isinstance(value, int):
        return {"type": "int", "integer_value": value}
    return {"type": "unicode", "string_value": str(value)}


def same_value(a: Any, b: Any) -> bool:
    """Equal, treating "6" and 6 alike (the edge document hands integers back as strings)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b or str(a) == str(b)


def plan_changes(current: dict[str, Any], release: dict[str, FlagValue]) -> list[FlagChange]:
    missing = sorted(set(release) - set(current))
    if missing:
        raise ReleaseError(f"flags not in the environment: {missing}")
    return [FlagChange(flag=f, old=current[f], new=release[f])
            for f in sorted(release) if not same_value(current[f], release[f])]


class AdminApi:
    def __init__(self, api_key: str, *, project_id: int, client: httpx.Client | None = None):
        if not api_key or api_key.startswith("ser."):
            raise ReleaseError("FLAGSMITH_API_KEY must be the organisation API key")
        self._key = api_key
        self.project_id = project_id
        self._http = client or httpx.Client(base_url=API_URL, timeout=30)
        self._features: dict[str, int] | None = None

    def __repr__(self) -> str:
        return f"AdminApi(project_id={self.project_id})"

    def _get(self, path: str) -> Any:
        r = self._http.get(path, headers={"Authorization": f"Api-Key {self._key}"})
        if r.status_code != 200:
            raise ReleaseError(f"GET {path} returned HTTP {r.status_code}")
        return r.json()

    def environment(self, name: str) -> dict[str, Any]:
        envs = self._get(f"/api/v1/environments/?project={self.project_id}")
        for env in envs.get("results", envs) if isinstance(envs, dict) else envs:
            if env["name"] == name:
                return env
        raise ReleaseError(f"unknown environment {name!r}")

    def feature_id(self, name: str) -> int:
        if self._features is None:
            rows = self._get(f"/api/v1/projects/{self.project_id}/features/?page_size=100")
            self._features = {r["name"]: r["id"] for r in rows.get("results", [])}
        if name not in self._features:
            raise ReleaseError(f"unknown feature {name!r}")
        return self._features[name]

    def admin_document(self, environment: str) -> dict[str, Any]:
        return self._get(f"/api/v1/environments/{self.environment(environment)['api_key']}"
                         "/document/")

    def publish_value(self, env_id: int, feature: str, value: FlagValue, note: str) -> str:
        fid = self.feature_id(feature)
        path = f"/api/v1/environments/{env_id}/features/{fid}/versions/"
        body = {"publish_immediately": True, "description": f"release.py: {note}",
                "feature_states_to_update": [{"feature": fid, "enabled": True,
                                              "feature_segment": None,
                                              "feature_state_value": typed_value(value)}]}
        r = self._http.post(path, json=body, headers={"Authorization": f"Api-Key {self._key}"})
        if r.status_code not in (200, 201):
            raise ReleaseError(f"publishing {feature} returned HTTP {r.status_code}: "
                               f"{r.text[:200]}")
        return r.json().get("uuid", "")


def _values(document: dict[str, Any]) -> dict[str, Any]:
    return {fs["feature"]["name"]: fs.get("feature_state_value")
            for fs in document.get("feature_states", [])}


def _without_keys(document: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in document.items() if k != "api_key"}


def snapshot(api: AdminApi, environment: str, *, server_key: str,
             edge_client: httpx.Client | None = None) -> dict[str, Any]:
    env = api.environment(environment)
    edge = edge_client or httpx.Client(base_url=EDGE_URL, timeout=30)
    r = edge.get("/api/v1/environment-document/", headers={"X-Environment-Key": server_key})
    if r.status_code != 200:
        raise ReleaseError(f"edge document returned HTTP {r.status_code}")
    edge_doc = r.json()
    if edge_doc.get("id") != env["id"]:
        raise ReleaseError(f"FLAGSMITH_SERVER_KEY belongs to another environment, "
                           f"not {environment}")
    admin_doc = api.admin_document(environment)
    edge_values, admin_values = _values(edge_doc), _values(admin_doc)
    match = edge_values.keys() == admin_values.keys() and all(
        same_value(edge_values[k], admin_values[k]) for k in edge_values)
    # Typed values come from the admin document (the edge turns ints into strings); the edge
    # document is the current one, so `admin_values_match` says whether admin has caught up.
    return {"environment": environment, "environment_id": env["id"],
            "taken_at": datetime.now(UTC).isoformat(), "values": admin_values,
            "edge_values": edge_values, "admin_values_match": match,
            "edge_document": _without_keys(edge_doc), "admin_document": _without_keys(admin_doc)}


def apply_values(api: AdminApi, environment: str, changes: list[FlagChange], *,
                 note: str) -> list[str]:
    env_id = api.environment(environment)["id"]
    return [api.publish_value(env_id, c.flag, c.new, note) for c in changes]


def _wait_until_shown(api: AdminApi, environment: str, server_key: str, want: dict[str, Any],
                      timeout: float = 150.0) -> dict[str, Any]:
    """Both documents lag a write (edge: seconds, admin: up to about a minute). Wait for both."""
    start = time.monotonic()
    while True:
        snap = snapshot(api, environment, server_key=server_key)
        shown = all(same_value(snap["values"].get(k), v)
                    and same_value(snap["edge_values"].get(k), v) for k, v in want.items())
        if shown:
            return snap
        if time.monotonic() - start > timeout:
            raise ReleaseError(f"Flagsmith did not show the new values within {timeout:.0f} s")
        time.sleep(3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--environment", required=True)
    ap.add_argument("--confirm-environment")
    ap.add_argument("--project-id", type=int, default=int(os.environ.get("FLAGSMITH_PROJECT_ID",
                                                                         "0")))
    ap.add_argument("--revert", type=Path, help="baseline_env.json to restore the values from")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("data/releases"))
    args = ap.parse_args(argv)

    try:
        check_environment_allowed(args.environment, args.confirm_environment)
        if not args.project_id:
            raise ReleaseError("set FLAGSMITH_PROJECT_ID or --project-id")
        server_key = os.environ.get("FLAGSMITH_SERVER_KEY", "")
        if not server_key.startswith("ser."):
            raise ReleaseError("FLAGSMITH_SERVER_KEY (ser. key of this environment) is not set")
        api = AdminApi(os.environ.get("FLAGSMITH_API_KEY", ""), project_id=args.project_id)

        before = snapshot(api, args.environment, server_key=server_key)
        if not before["admin_values_match"]:
            raise ReleaseError("Flagsmith is still settling a recent change (admin and edge "
                               "documents differ); try again in a minute")
        if args.revert:
            saved = json.loads(args.revert.read_text())
            if saved.get("environment") != args.environment:
                raise ReleaseError("that baseline belongs to another environment")
            target = {k: saved["values"][k] for k in PLAN_RELEASE}
        else:
            target = dict(PLAN_RELEASE)
        changes = plan_changes(before["values"], target)
        for c in changes:
            print(f"  {c.flag}: {c.old!r} -> {c.new!r}")
        if args.dry_run or not changes:
            print("dry run, nothing written" if args.dry_run else "nothing to change")
            return 0

        folder = args.out / args.environment / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        folder.mkdir(parents=True, exist_ok=True)
        label = "revert" if args.revert else "release"
        (folder / f"{'pre_revert' if args.revert else 'baseline'}_env.json").write_text(
            json.dumps(before, indent=2, sort_keys=True))
        uuids = apply_values(api, args.environment, changes, note=f"{label} {folder.name}")
        after = _wait_until_shown(api, args.environment, server_key, target)
        (folder / f"{label}_env.json").write_text(json.dumps(after, indent=2, sort_keys=True))
        (folder / f"{label}.json").write_text(json.dumps(
            {"environment": args.environment, "versions": uuids,
             "changes": [c.model_dump(mode="json") for c in changes]}, indent=2))
        print(f"{label}: {len(changes)} flags changed; snapshots in {folder}")
        return 0
    except ReleaseError as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
