"""Read-back after an approved write: does Flagsmith now serve what we expect, per identity?

The verifier resolves each test identity (e.g. a tier-0 production cluster and a staging one)
with the server-side environment key only, and polls until every expected value is served or
the timeout passes (60 s by default; the edge lags a write by a few seconds).

- VERIFIED: every identity got its expected value, from Flagsmith itself.
- FAILED:   Flagsmith answered, but with a different value until the timeout.
- TIMEOUT:  some value never came from Flagsmith (unreachable, or served by a default).

A value from a default handler (`is_default`) never counts, so a network blip can't pass as a
verified fix. The source here is the edge identities endpoint (remote evaluation, transient
identity, nothing stored). It matches what the Flagsmith SDK's local evaluation resolves.
The Python SDK isn't a dependency yet, and anything that implements FlagSource can replace this.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import httpx

from contracts.models import FlagValue, Identity, ReadBack

EDGE_IDENTITIES_URL = "https://edge.api.flagsmith.com/api/v1/identities/"


class VerifyStatus(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class ServedFlag:
    value: Any
    enabled: bool
    is_default: bool


class SourceUnavailable(Exception):
    pass


class FlagSource(Protocol):
    def identity_flags(self, identity: Identity) -> dict[str, ServedFlag]: ...


class EdgeFlagSource:
    def __init__(self, server_key: str, *, client: httpx.Client | None = None,
                 timeout: float = 10.0):
        if not server_key.startswith("ser."):
            raise ValueError("the verifier needs a server-side (ser.) environment key")
        self._key = server_key
        self._http = client or httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return "EdgeFlagSource()"

    def identity_flags(self, identity: Identity) -> dict[str, ServedFlag]:
        body = {"identifier": identity.identifier, "transient": True,
                "traits": [{"trait_key": k, "trait_value": v} for k, v in identity.traits.items()]}
        try:
            r = self._http.post(EDGE_IDENTITIES_URL, json=body,
                                headers={"X-Environment-Key": self._key})
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"edge unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise SourceUnavailable(f"edge identities returned HTTP {r.status_code}")
        try:
            flags = r.json()["flags"]
        except (ValueError, KeyError, TypeError):
            raise SourceUnavailable("edge identities returned an unexpected body") from None
        return {f["feature"]["name"]: ServedFlag(f.get("feature_state_value"),
                                                 bool(f.get("enabled")), False) for f in flags}


@dataclass(frozen=True)
class Expectation:
    identity: Identity
    values: dict[str, FlagValue]


@dataclass
class VerifyResult:
    status: VerifyStatus
    readbacks: list[ReadBack] = field(default_factory=list)
    elapsed_s: float = 0.0
    detail: str = ""


def _as_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _same(served: Any, expected: Any) -> bool:
    if isinstance(served, bool) or isinstance(expected, bool):
        return served is expected
    if served == expected:
        return True
    # Flagsmith hands integers back as strings in some paths ("6" vs 6)
    a, b = _as_int(served), _as_int(expected)
    return a is not None and a == b


def _read(expectation: Expectation, source: FlagSource) -> tuple[ReadBack, bool, list[str]]:
    """One identity: its read-back, whether it is fully right, and what is wrong."""
    try:
        flags = source.identity_flags(expectation.identity)
    except SourceUnavailable:
        flags = {}
    values, from_default, wrong = {}, False, []
    for name, want in expectation.values.items():
        got = flags.get(name)
        if got is None or got.is_default:
            from_default = True
            wrong.append(f"{expectation.identity.identifier}: {name} not served by Flagsmith")
            continue
        values[name] = got.value
        if not _same(got.value, want):
            wrong.append(f"{expectation.identity.identifier}: {name}={got.value!r}, "
                         f"expected {want!r}")
    readback = ReadBack(identity=expectation.identity, values=values,
                        from_default_handler=from_default)
    return readback, not wrong, wrong


def verify(expectations: Sequence[Expectation], source: FlagSource, *, timeout_s: float = 60.0,
           interval_s: float = 3.0, clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> VerifyResult:
    if not expectations:
        raise ValueError("nothing to verify")
    start = clock()
    while True:
        reads = [_read(e, source) for e in expectations]
        readbacks = [r[0] for r in reads]
        elapsed = clock() - start
        if all(ok for _, ok, _ in reads):
            return VerifyResult(VerifyStatus.VERIFIED, readbacks, elapsed)
        if elapsed >= timeout_s:
            problems = [p for _, _, wrong in reads for p in wrong]
            status = (VerifyStatus.TIMEOUT if any(r.from_default_handler for r in readbacks)
                      else VerifyStatus.FAILED)
            return VerifyResult(status, readbacks, elapsed, "; ".join(problems))
        sleep(interval_s)
