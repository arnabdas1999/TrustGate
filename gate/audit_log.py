"""The gate's append-only audit trail (JSONL), served to the investigator as get_audit_log.

Every read, refusal and write the gate handles is one line. Lines are only ever appended, and
each carries `prev_hash` (SHA-256 of the previous line), so an edited or deleted line breaks
`verify_chain`. Each line also validates as contracts.AuditEntry, so the API can show it as is.

Flagsmith's own audit log is empty on our plan, and v2 feature versions record no author for
API-key writes. So this file is the record of who approved what; `merge_versions` adds
Flagsmith's "when" (the version's live_from), matched by version uuid. A version counts as
TrustGate's only if its uuid is in this log: descriptions are free text anyone can write.

One AuditLog instance owns the file per process (the gate service). It keeps the last line's
hash in memory, so appending never re-reads the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ACTIONS = frozenset({"read", "write", "refused", "approve", "reject"})
_SECRET_PATTERNS = [re.compile(r"ser\.[A-Za-z0-9_-]{6,}"),
                    re.compile(r"(Api-Key\s+)\S+", re.IGNORECASE)]
GENESIS = "0" * 64


class AuditTampered(Exception):
    pass


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", value)
        return value
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _parses(line: str) -> bool:
    if not line.strip():
        return False
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{dt.microsecond:06d}Z" if dt.microsecond else "Z")


class AuditLog:
    def __init__(self, path: str | Path, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._last: str | None = None  # hash of the last complete line, once known

    def _load_last_hash(self) -> str:
        """Read the file once: hash of the last complete line, ending any half-written one."""
        if not self.path.exists():
            return GENESIS
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            with self.path.open("ab") as fh:  # a crash mid-write: close that line off
                fh.write(b"\n")
        last = ""
        for line in raw.decode("utf-8", "replace").splitlines():
            if _parses(line):
                last = line.strip()
        return hashlib.sha256(last.encode()).hexdigest() if last else GENESIS

    def append(self, *, actor: str, action: str, tool: str | None, args: dict[str, Any],
               result: str | None, proposal_id: str | None = None, **extra: Any) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError(f"unknown audit action {action!r}")
        with self._lock:
            if self._last is None:
                self._last = self._load_last_hash()
            entry = {"ts": _ts(self._clock()), "actor": actor, "action": action, "tool": tool,
                     "args": _mask(args), "result": _mask(result), "proposal_id": proposal_id,
                     **_mask(extra), "prev_hash": self._last}
            line = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last = hashlib.sha256(line.encode()).hexdigest()
        return entry

    def entries(self, proposal_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text("utf-8").splitlines()
                if _parses(line)]  # a half-written last line is skipped
        return [r for r in rows if proposal_id is None or r.get("proposal_id") == proposal_id]

    def verify_chain(self) -> None:
        if not self.path.exists():
            return
        expected = GENESIS
        for n, line in enumerate(self.path.read_text("utf-8").splitlines(), start=1):
            if not _parses(line):  # blank, or a line cut off by a crash
                continue
            if json.loads(line).get("prev_hash") != expected:
                raise AuditTampered(f"audit line {n} does not follow line {n - 1}")
            expected = hashlib.sha256(line.strip().encode()).hexdigest()


def merge_versions(entries: list[dict[str, Any]], versions: list[dict[str, Any]], *,
                   known_entries: list[dict[str, Any]] | None = None,
                   ) -> dict[str, list[dict[str, Any]]]:
    """Attach Flagsmith's live_from to gate writes; list versions TrustGate did not make.

    `entries` may be filtered (e.g. one proposal); `known_entries` should be the whole log,
    so writes for other proposals are still recognised as TrustGate's.
    """
    by_uuid = {v.get("uuid"): v for v in versions}
    ours = {u for e in (known_entries or []) for u in (e.get("version_uuids") or [])}
    merged = []
    for entry in entries:
        entry = dict(entry)
        uuids = entry.get("version_uuids") or []
        if uuids:
            entry["flagsmith_live_from"] = [by_uuid[u].get("live_from") for u in uuids
                                            if u in by_uuid]
            ours.update(uuids)
        merged.append(entry)
    # Only uuids the gate itself recorded count as TrustGate's; descriptions are untrusted.
    outside = [v for v in versions if v.get("uuid") not in ours]
    return {"gate": merged, "outside_trustgate": outside}
