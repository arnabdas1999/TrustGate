"""Snapshots and the release diff: the old and new value of every flag the release changed.

An incident names two snapshots by reference:
- "file:<path>": a snapshot saved by scripts/release.py (typed values from the admin document)
- "gate:<environment>": the environment as it is now, read through the gate
  (get_environment_document, an allowlisted read)
The diff between them is the suspect list. The audit log is never parsed for values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contracts.models import FlagChange


class SnapshotError(Exception):
    pass


def load_snapshot(ref: str, gate: Any) -> dict[str, Any]:
    scheme, _, rest = ref.partition(":")
    if not rest:
        raise SnapshotError(f"bad snapshot reference {ref!r}")
    if scheme == "file":
        try:
            return json.loads(Path(rest).read_text())["values"]
        except (OSError, ValueError, KeyError) as exc:
            raise SnapshotError(f"can't read snapshot {rest}: {type(exc).__name__}") from None
    if scheme == "gate":
        view = gate.read("get_environment_document", {"environment": rest})["view"]
        return {name: state["value"] for name, state in view["features"].items()}
    raise SnapshotError(f"unknown snapshot scheme in {ref!r}")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b or (a is not None and b is not None and str(a) == str(b))


def release_diff(baseline: dict[str, Any], release: dict[str, Any]) -> list[FlagChange]:
    """Changed flags, by name. "6" and 6 are the same value (the edge returns ints as strings)."""
    return [FlagChange(flag=f, old=baseline.get(f), new=release.get(f))
            for f in sorted(set(baseline) | set(release))
            if not _same(baseline.get(f), release.get(f))]


def segments_document(view: dict[str, Any]) -> dict[str, Any]:
    """The gate's canonical view, reshaped for agent.triage."""
    return {"project": {"segments": [{"name": name, "rules": seg["rules"]}
                                     for name, seg in sorted(view["segments"].items())]}}
