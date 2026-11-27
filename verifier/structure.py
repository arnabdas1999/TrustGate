"""Structural check after an approved write: is the write really in the environment document?

The edge read-back (readback.py) proves what identities are served. On a bench environment the
repair value can equal what the segment member is served anyway (the release only ran in the
sandbox), so the read-back alone can't tell a landed override from no write at all. This check
reads the environment document through the gate (get_environment_document, the canonical view)
and confirms each op:

- create_segment_override: segments[S]["overrides"][feature] exists with the op's value and
  enabled state (the writer sends enabled=True unless the op says otherwise)
- update_feature_state:    features[feature]["value"] equals the op's value (and enabled, if set)
- delete_feature_segment:  segments[S]["overrides"] no longer has the feature

Values compare loosely ("6" == 6), like the release diff. The document lags a write by a few
seconds, so confirm_written polls with the same timeout and interval as verify(). A gate read
that keeps failing is TIMEOUT (unverified), never VERIFIED.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.models import WriteOp
from orchestrator.snapshots import same_value
from verifier.readback import VerifyStatus


@dataclass
class StructureResult:
    status: VerifyStatus
    detail: str = ""


def _enabled(op: WriteOp) -> bool:
    return True if op.enabled is None else op.enabled


def structure_problems(ops: Sequence[WriteOp], view: dict[str, Any]) -> list[str]:
    """Why the canonical view doesn't show `ops` as written (empty list = all present)."""
    features = view.get("features") or {}
    segments = view.get("segments") or {}
    problems = []
    for op in ops:
        if op.tool == "create_segment_override":
            override = ((segments.get(op.segment) or {}).get("overrides") or {}).get(op.feature)
            if override is None:
                problems.append(f"override for {op.feature} on {op.segment} not found in the "
                                "environment document")
            elif not same_value(override.get("value"), op.value):
                problems.append(f"override for {op.feature} on {op.segment} is "
                                f"{override.get('value')!r}, expected {op.value!r}")
            elif bool(override.get("enabled")) is not _enabled(op):
                problems.append(f"override for {op.feature} on {op.segment} has enabled="
                                f"{override.get('enabled')}, expected {_enabled(op)}")
        elif op.tool == "update_feature_state":
            state = features.get(op.feature)
            if state is None:
                problems.append(f"{op.feature} not found in the environment document")
            elif not same_value(state.get("value"), op.value):
                problems.append(f"{op.feature} default is {state.get('value')!r}, expected "
                                f"{op.value!r}")
            elif op.enabled is not None and bool(state.get("enabled")) is not op.enabled:
                problems.append(f"{op.feature} default has enabled={state.get('enabled')}, "
                                f"expected {op.enabled}")
        elif op.tool == "delete_feature_segment":
            overrides = (segments.get(op.segment) or {}).get("overrides") or {}
            if op.feature in overrides:
                problems.append(f"override for {op.feature} on {op.segment} is still in the "
                                "environment document")
        else:
            problems.append(f"no structural check for {op.tool}")
    return problems


def confirm_written(ops: Sequence[WriteOp], read_view: Callable[[], dict[str, Any]], *,
                    timeout_s: float = 60.0, interval_s: float = 3.0,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> StructureResult:
    """Poll the environment document until every op is visible, or the timeout passes."""
    start = clock()
    while True:
        try:
            problems = structure_problems(ops, read_view())
            read_error = None
        except Exception as exc:  # noqa: BLE001 (gate down or a bad body: retry, then unverified)
            problems, read_error = [], f"{type(exc).__name__}: {exc}"
        if not problems and read_error is None:
            return StructureResult(VerifyStatus.VERIFIED)
        if clock() - start >= timeout_s:
            if read_error is not None:
                return StructureResult(VerifyStatus.TIMEOUT, "could not read the environment "
                                       f"document to confirm the write ({read_error})")
            return StructureResult(VerifyStatus.FAILED, "; ".join(problems))
        sleep(interval_s)
