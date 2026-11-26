import hashlib
import io

from fastapi.testclient import TestClient

from gate.approvals import ApprovalStore
from gate.audit_log import AuditLog
from gate.mcp_client import ToolResult
from gate.scope import GateScope
from gate.service import GateDeps, create_app
from gate.writer import StaticIds
from scripts.security_beats import Beats

SECRET = "beats-secret"
SCOPE = GateScope(project_id=56664, project_name="OpsPilot", environments={"Development": 121197})
IDS = StaticIds(features={"agent_prompt_version": 306127, "agent_tool_policy": 306128},
                segments={"tier0_production": 1205876})


class FakeFlagsmith:
    """Each write changes the environment's state; the state's fingerprint follows it."""

    def __init__(self):
        self.overrides: set[tuple[int, int]] = set()
        self.writes = []

    def fingerprint(self, env):
        return hashlib.sha256(repr(sorted(self.overrides)).encode()).hexdigest()

    def call_tool(self, name, args, *, is_write):
        if is_write:
            self.writes.append(args)
            for fs in args.get("feature_states_to_create", []):
                self.overrides.add((args["feature_pk"], fs["feature_segment"]["segment"]))
            for seg in args.get("segment_ids_to_delete_overrides", []):
                self.overrides.discard((args["feature_pk"], seg))
            return ToolResult(False, [{"type": "text", "text": '{"uuid": "v"}'}])
        return ToolResult(False, [{"type": "text", "text": "{}"}])


def _setup(tmp_path):
    flagsmith = FakeFlagsmith()
    deps = GateDeps(scope=SCOPE, mcp=flagsmith, ids=IDS,
                    approvals=ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE),
                    audit=AuditLog(tmp_path / "audit.jsonl"), fingerprint=flagsmith.fingerprint,
                    document=lambda env: {}, settle_reads=2, sleep=lambda s: None)
    client = TestClient(create_app(deps, shared_token=""), base_url="http://gate")
    return flagsmith, client


def test_beats_show_three_refusals_and_leave_no_trace(tmp_path):
    flagsmith, client = _setup(tmp_path)
    start = flagsmith.fingerprint("Development")
    out = io.StringIO()
    beats = Beats(client, secret=SECRET, environment="Development", project="OpsPilot",
                  out=out, sleep=lambda s: None)
    assert beats.run() == 0
    text = out.getvalue()
    assert "403 reused" in text
    assert "409 stale" in text
    assert "403 denied" in text and "list_organizations" in text
    assert "back at its starting state" in text
    assert flagsmith.fingerprint("Development") == start  # cleaned up
    # Exactly two writes happened: the approved override and its cleanup.
    assert len(flagsmith.writes) == 2
    assert SECRET not in text


def test_beats_refuse_anything_but_development(tmp_path):
    _, client = _setup(tmp_path)
    out = io.StringIO()
    beats = Beats(client, secret=SECRET, environment="Demo Production", project="OpsPilot",
                  out=out, sleep=lambda s: None)
    assert beats.run() == 2 and "Development only" in out.getvalue()
