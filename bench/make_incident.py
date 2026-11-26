"""Generate a blind benchmark incident: a flag release with a hidden culprit.

    uv run python -m bench.make_incident --domain opspilot --seed 1 --set dev

Writes:
  bench/releases/<domain>-<set>-<seed>.json  the release (flag changes only, culprit not marked)
  bench/keys/<domain>-<set>-<seed>.json      the sealed answer key (git-ignored until eval ends)
  bench/keys/hashes.txt                      one SHA-256 per eval key, committed now as proof
  bench/keys/hashes-dev.txt                  dev-case hashes (git-ignored, never reported)

The key carries a random salt, so nobody can recover the culprit by hashing guesses.
Prints only the release path and the key hash. Never print or open the key while timing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
from pathlib import Path
from typing import Any

from contracts import canonical_json

BENCH_DIR = Path(__file__).resolve().parent
TARGET_CHANGES = (4, 5)  # total flag changes per release, like a real bundled release


class PoolError(ValueError):
    pass


def load_pools(path: Path = BENCH_DIR / "pools.json") -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _flags(entry: dict[str, Any]) -> set[str]:
    return {c["flag"] for c in entry["changes"]}


def build_release(pools: dict[str, Any], domain: str, seed: int,
                  allow_unverified: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (release changes, key). Deterministic for a given pools file, domain and seed."""
    if domain not in pools:
        raise PoolError(f"unknown domain {domain!r}")
    pool = pools[domain]
    baseline = pool["baseline"]

    def usable(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in entries if allow_unverified or e.get("verified")]

    bad_pool, safe_pool = usable(pool["bad"]), usable(pool["safe"])
    if not bad_pool:
        raise PoolError(
            f"{domain}: no verified bad changes. Verify them first, or pass --allow-unverified "
            "for dev cases only."
        )

    rng = random.Random(f"{domain}:{seed}")  # noqa: S311 (reproducible shuffling, not security)
    bad = rng.choice(bad_pool)
    used = _flags(bad)
    target = rng.choice(TARGET_CHANGES)
    chosen = [bad]
    for entry in rng.sample(safe_pool, k=len(safe_pool)):
        if len(used) >= target:
            break
        if _flags(entry) & used:
            continue
        chosen.append(entry)
        used |= _flags(entry)

    changes = [
        {"flag": c["flag"], "old": baseline[c["flag"]], "new": c["new"]}
        for entry in chosen
        for c in entry["changes"]
    ]
    for change in changes:
        if change["flag"] not in baseline:
            raise PoolError(f"{domain}: flag {change['flag']!r} is not in the baseline")
    rng.shuffle(changes)

    key = {
        "domain": domain,
        "seed": seed,
        "bad_id": bad["id"],
        "bad_changes": [{"flag": c["flag"], "new": c["new"]} for c in bad["changes"]],
        "safe_ids": [e["id"] for e in chosen[1:]],
    }
    return changes, key


def seal(key: dict[str, Any]) -> tuple[dict[str, Any], str]:
    sealed = {**key, "salt": secrets.token_hex(16)}
    digest = hashlib.sha256(canonical_json(sealed).encode("utf-8")).hexdigest()
    return sealed, digest


def write_incident(
    domain: str,
    seed: int,
    case_set: str,
    allow_unverified: bool = False,
    bench_dir: Path = BENCH_DIR,
    pools: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    pools = pools if pools is not None else load_pools(bench_dir / "pools.json")
    changes, key = build_release(pools, domain, seed, allow_unverified)
    sealed, digest = seal({**key, "set": case_set})
    name = f"{domain}-{case_set}-{seed}"

    releases, keys = bench_dir / "releases", bench_dir / "keys"
    releases.mkdir(parents=True, exist_ok=True)
    keys.mkdir(parents=True, exist_ok=True)
    key_path = keys / f"{name}.json"
    if key_path.exists():
        raise FileExistsError(f"{key_path} already exists: pick another seed")

    release = {"case": name, "domain": domain, "set": case_set, "seed": seed,
               "changes": changes, "key_sha256": digest}
    release_path = releases / f"{name}.json"
    release_path.write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8", newline="\n")
    key_path.write_text(canonical_json(sealed) + "\n", encoding="utf-8", newline="\n")
    # Only eval hashes go in the committed hashes.txt (the pre-registration proof).
    # Dev hashes go in hashes-dev.txt, which is git-ignored.
    hashes = keys / ("hashes.txt" if case_set == "eval" else "hashes-dev.txt")
    with hashes.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{name} {digest}\n")
    return release_path, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", choices=["opspilot", "vaultguard"], required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--set", dest="case_set", choices=["dev", "eval"], default="dev")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="use unverified pool entries (dev cases only)")
    args = parser.parse_args()
    if args.case_set == "eval" and args.allow_unverified:
        parser.error("eval cases must use verified pool entries only")
    path, digest = write_incident(args.domain, args.seed, args.case_set, args.allow_unverified)
    print(f"release: {path}")
    print(f"key sha256: {digest}")


if __name__ == "__main__":
    main()
