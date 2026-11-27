"""Check that sandbox/flags.py SEGMENTS behave like the segments in Flagsmith.

The sandbox validates segment overrides with its own copy of the segment rules. If Flagsmith's
rules differ, a repair could be validated under rules production doesn't use. This reads the
edge environment document with the server-side key (ser., read only) and evaluates both copies
on a grid of probe identities. It compares behaviour, not JSON shape, so equivalent rules
written differently still match.

    uv run --env-file .env python -m sandbox.check_segments --domain opspilot
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from itertools import product
from typing import Any

import httpx

from agent.triage import segment_matches, segments_of
from sandbox.flags import SEGMENTS

EDGE_DOCUMENT_URL = "https://edge.api.flagsmith.com/api/v1/environment-document/"
DOMAIN_SEGMENTS = {"opspilot": ["tier0_production"], "vaultguard": ["high_value_wires"]}
SERVER_KEY_VARS = {"opspilot": "FLAGSMITH_SERVER_KEY_OPSPILOT_PROD",
                   "vaultguard": "FLAGSMITH_SERVER_KEY_VAULTGUARD_PROD"}
PROBES: dict[str, list[dict[str, Any]]] = {
    "opspilot": [{"env": e, "tier": t}
                 for e, t in product(["prod", "staging", "dev"], [0, 1, 2, 3])],
    "vaultguard": [{"amount_usd": a, "new_vendor": n}
                   for a, n in product([0, 400, 9999, 9999.99, 10000, 12000, 25000],
                                       [True, False])],
}


class CheckError(Exception):
    pass


def fetch_document(server_key: str, client: httpx.Client | None = None) -> dict[str, Any]:
    if not server_key.startswith("ser."):
        raise CheckError("needs a server-side environment key (ser.)")
    http = client or httpx.Client(timeout=15)
    try:
        resp = http.get(EDGE_DOCUMENT_URL, headers={"X-Environment-Key": server_key})
    except httpx.HTTPError as exc:
        raise CheckError(f"edge document unreachable: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise CheckError(f"edge document HTTP {resp.status_code}")
    return resp.json()


def compare(domain: str, document: Mapping[str, Any]) -> list[str]:
    """Problems found; empty means the sandbox's segments behave like Flagsmith's."""
    live = {s["name"]: s for s in segments_of(document)}
    problems = []
    for name in DOMAIN_SEGMENTS[domain]:
        if name not in live:
            problems.append(f"{name}: not in Flagsmith (has {sorted(live) or 'no segments'})")
            continue
        for traits in PROBES[domain]:
            ours, theirs = segment_matches(SEGMENTS[name], traits), \
                segment_matches(live[name], traits)
            if ours != theirs:
                problems.append(f"{name}: {traits} -> sandbox {ours}, Flagsmith {theirs}")
    return problems


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--domain", default="opspilot", choices=sorted(DOMAIN_SEGMENTS))
    p.add_argument("--key-var", help="env var holding the ser. key (default per domain)")
    args = p.parse_args(argv)
    var = args.key_var or SERVER_KEY_VARS[args.domain]
    key = os.environ.get(var, "")
    if not key:
        print(f"{var} is not set", file=sys.stderr)
        return 2
    try:
        problems = compare(args.domain, fetch_document(key, client))
    except CheckError as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("MISMATCH between sandbox/flags.py SEGMENTS and Flagsmith:")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print(f"OK: {', '.join(DOMAIN_SEGMENTS[args.domain])} behave the same on "
          f"{len(PROBES[args.domain])} probe identities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
