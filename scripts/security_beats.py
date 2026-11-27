"""Security beats for the demo video: three things the gate refuses, against a live gate.

    GATE_URL=http://localhost:8100 APPROVAL_SIGNING_SECRET=... \\
        uv run python -m scripts.security_beats

1. A reused approval is refused (403 reused).
2. A stale proposal is refused (409): Flagsmith changed after the proposal was registered.
3. The investigator asking for a non-allowlisted read is refused (403).

Development only. It plays the API's role (it signs approvals with the shared secret), makes
exactly two writes (an approved tier-0 override and the cleanup that removes it) and leaves
the environment at the fingerprint it started from. The secret is never printed.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

import httpx

from contracts.hashing import payload_hash, sign_approval
from contracts.models import Approval, WriteOp

ENVIRONMENT = "Development"
SEGMENT = "tier0_production"


class BeatError(Exception):
    pass


class Beats:
    def __init__(self, client: httpx.Client, *, secret: str, environment: str, project: str,
                 out: TextIO = sys.stdout, sleep: Callable[[float], None] = time.sleep,
                 token: str = ""):
        self.http, self.secret, self.env, self.project = client, secret, environment, project
        self.out, self.sleep = out, sleep
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")
        self.out.flush()

    # -- gate calls -------------------------------------------------------------------------

    def _op(self, tool: str, feature: str, value: Any = None) -> WriteOp:
        return WriteOp(tool=tool, project=self.project, environment=self.env, feature=feature,
                       segment=SEGMENT, value=value)

    def fingerprint(self) -> str:
        r = self.http.get("/gate/fingerprint", params={"environment": self.env},
                          headers=self.headers)
        r.raise_for_status()
        return r.json()["fingerprint"]

    def register(self, ops: list[WriteOp]) -> tuple[str, str]:
        proposal_id = f"prop_beat_{secrets.token_hex(4)}"
        body = {"proposal_id": proposal_id, "ops": [o.model_dump(mode="json") for o in ops]}
        for _ in range(15):  # 503 while the edge still shows the state before a write
            r = self.http.post("/gate/register", json=body, headers=self.headers)
            if r.status_code != 503:
                break
            self.sleep(2)
        if r.status_code != 200:
            raise BeatError(f"register failed: HTTP {r.status_code} {r.text[:200]}")
        return proposal_id, r.json()["fingerprint"]

    def approve(self, proposal_id: str, ops: list[WriteOp]) -> dict[str, Any]:
        """Sign exactly as the API does (single use, 10-minute expiry)."""
        approval_id = f"appr_beat_{secrets.token_hex(4)}"
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=10)
        ph = payload_hash(ops)
        approval = Approval(proposal_id=proposal_id, approval_id=approval_id,
                            approver="demo-approver", payload_hash=ph, created_at=now,
                            expires_at=expires,
                            signature=sign_approval(self.secret, approval_id, proposal_id, ph,
                                                    expires.isoformat()))
        return {"approval": approval.model_dump(mode="json"),
                "ops": [o.model_dump(mode="json") for o in ops]}

    def write(self, body: dict[str, Any]) -> httpx.Response:
        return self.http.post("/gate/write", json=body, headers=self.headers, timeout=60)

    @staticmethod
    def _why(r: httpx.Response) -> str:
        detail = r.json().get("detail")
        if isinstance(detail, dict):
            return f"{detail.get('reason')}: {detail.get('detail')}"
        return f"stale: {detail}" if r.status_code == 409 else str(detail)

    # -- the beats --------------------------------------------------------------------------

    def run(self) -> int:
        if self.env != ENVIRONMENT:
            self.say(f"security_beats runs on {ENVIRONMENT} only, not {self.env!r}.")
            return 2
        start = self.fingerprint()
        override = [self._op("create_segment_override", "agent_prompt_version", "v1_strict")]
        cleanup = [self._op("delete_feature_segment", "agent_prompt_version")]
        applied = cleaned = stale_applied = False
        stale_cleanup = [self._op("delete_feature_segment", "agent_tool_policy")]
        self.say(f"TrustGate security beats ({self.env}, starting state {start[:8]})")
        self.say("")
        try:
            # 1. Reused approval
            pid, _ = self.register(override)
            body = self.approve(pid, override)
            r = self.write(body)
            if r.status_code != 200:
                raise BeatError(f"the approved write failed: HTTP {r.status_code} {r.text}")
            applied = True
            self.say("[1] Approved change applied: tier0_production override "
                     "agent_prompt_version = v1_strict")
            r = self.write(body)
            self.say(f"[1] Same approval sent again -> {r.status_code} {self._why(r)}")
            self.say("")

            # 2. Stale proposal: Flagsmith changes after B is registered
            stale_ops = [self._op("create_segment_override", "agent_tool_policy", "restricted")]
            stale_pid, stale_fp = self.register(stale_ops)
            self.say(f"[2] Proposal B registered against state {stale_fp[:8]}")
            clean_pid, _ = self.register(cleanup)
            r = self.write(self.approve(clean_pid, cleanup))
            if r.status_code != 200:
                raise BeatError(f"cleanup write failed: HTTP {r.status_code} {r.text}")
            cleaned = True
            self.say("[2] Meanwhile Flagsmith changes (another approved change lands)")
            r = self.write(self.approve(stale_pid, stale_ops))
            if r.status_code == 200:  # should never happen; undo it in finally
                stale_applied = True
                raise BeatError("the stale proposal was written; the gate must refuse it")
            self.say(f"[2] B approved anyway -> {r.status_code} {self._why(r)}. Nothing written.")
            self.say("")

            # 3. Denied read
            r = self.http.post("/gate/read", json={"tool": "list_organizations", "args": {}},
                               headers=self.headers)
            self.say(f"[3] Investigator asks for list_organizations -> {r.status_code} denied: "
                     f"{r.json().get('detail')}")
            self.say("")
        finally:
            if applied and not cleaned:  # never leave the override behind
                clean_pid, _ = self.register(cleanup)
                self.write(self.approve(clean_pid, cleanup))
            if stale_applied:
                clean_pid, _ = self.register(stale_cleanup)
                self.write(self.approve(clean_pid, stale_cleanup))
        for _ in range(30):
            if self.fingerprint() == start:
                break
            self.sleep(2)
        back = self.fingerprint() == start
        self.say(f"{self.env} back at its starting state: {'yes' if back else 'NO'} "
                 f"({start[:8]})")
        return 0 if back else 1


def main() -> int:
    secret = os.environ.get("APPROVAL_SIGNING_SECRET", "")
    if not secret:
        print("APPROVAL_SIGNING_SECRET is not set (it must match the gate's).", file=sys.stderr)
        return 2
    base = os.environ.get("GATE_URL", "http://localhost:8100").rstrip("/")
    with httpx.Client(base_url=base, timeout=30) as client:
        beats = Beats(client, secret=secret,
                      environment=os.environ.get("BEATS_ENVIRONMENT", ENVIRONMENT),
                      project=os.environ.get("GATE_PROJECT_NAME", "TrustGate"),
                      token=os.environ.get("GATE_SHARED_TOKEN", "").strip())
        try:
            return beats.run()
        except BeatError as exc:
            print(f"security_beats: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
