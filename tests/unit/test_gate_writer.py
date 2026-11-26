import json

import pytest

from contracts.models import WriteOp
from gate.mcp_client import ToolResult
from gate.scope import GateScope
from gate.writer import (
    V2_WRITE_TOOL,
    McpCall,
    McpIdResolver,
    StaticIds,
    WriterError,
    render,
    send,
)

SCOPE = GateScope(project_id=56664, project_name="OpsPilot",
                  environments={"Development": 121197, "Demo Production": 121198})
IDS = StaticIds(features={"agent_prompt_version": 306127, "agent_tool_policy": 306128,
                          "agent_max_steps": 306129, "agent_safe_fallback": 306132},
                segments={"tier0_production": 1205876, "high_value_wires": 77})


def op(tool="create_segment_override", feature="agent_prompt_version", segment="tier0_production",
       value="v1_strict", environment="Development", enabled=None):
    return WriteOp(tool=tool, project="OpsPilot", environment=environment, feature=feature,
                   segment=segment, value=value, enabled=enabled)


def _render(ops):
    return render(ops, SCOPE, IDS, proposal_id="prop_1", approval_id="appr_1")


def test_segment_override_renders_to_one_v2_call():
    calls = _render([op()])
    assert calls == [McpCall(V2_WRITE_TOOL, {
        "environment_pk": 121197,
        "feature_pk": 306127,
        "publish_immediately": True,
        "description": "trustgate proposal=prop_1 approval=appr_1",
        "feature_states_to_create": [{
            "feature": 306127, "enabled": True,
            "feature_segment": {"segment": 1205876},
            "feature_state_value": {"type": "unicode", "string_value": "v1_strict"},
        }],
    })]


def test_remove_override_renders_segment_ids_to_delete():
    calls = _render([op(tool="delete_feature_segment", value=None)])
    assert calls[0].arguments["segment_ids_to_delete_overrides"] == [1205876]
    assert "feature_states_to_create" not in calls[0].arguments


@pytest.mark.parametrize(("value", "expected"), [
    ("restricted", {"type": "unicode", "string_value": "restricted"}),
    (6, {"type": "int", "integer_value": 6}),
    (False, {"type": "bool", "boolean_value": False}),
    (0.0, {"type": "unicode", "string_value": "0.0"}),
])
def test_environment_default_renders_feature_states_to_update(value, expected):
    calls = _render([op(tool="update_feature_state", segment=None, value=value,
                        feature="agent_max_steps")])
    assert calls[0].arguments["feature_states_to_update"] == [{
        "feature": 306129, "enabled": True, "feature_segment": None,
        "feature_state_value": expected}]


def test_one_call_per_feature_in_a_stable_order():
    ops = [op(tool="update_feature_state", segment=None, feature="agent_tool_policy",
              value="restricted"),
           op(feature="agent_prompt_version")]
    calls = _render(ops)
    assert [c.arguments["feature_pk"] for c in calls] == [306127, 306128]
    assert _render(list(reversed(ops))) == calls


def test_same_input_renders_identically():
    assert _render([op()]) == _render([op()])


def test_explicit_enabled_is_kept():
    call = _render([op(enabled=False)])[0]
    assert call.arguments["feature_states_to_create"][0]["enabled"] is False


@pytest.mark.parametrize("ops", [
    [],
    [op(), op(environment="Demo Production")],
    [op(environment="Production")],
    [op(feature="unknown_flag")],
    [op(segment="unknown_segment")],
    [op(), op(value="v2_fast")],
    [op(), op(tool="delete_feature_segment", value=None)],
    [op(value=None)],
    [op(tool="update_feature_state", segment=None, value=None)],
    [op(tool="update_feature_state", segment=None, value=2**40, feature="agent_max_steps")],
], ids=["empty", "two environments", "out of scope env", "unknown feature", "unknown segment",
        "same segment twice", "create and delete same segment", "override without value",
        "default without value", "int too big"])
def test_bad_op_lists_are_refused(ops):
    with pytest.raises(WriterError):
        _render(ops)


def test_mcp_call_to_dict_is_the_exact_call():
    call = _render([op()])[0]
    assert call.to_dict() == {"tool": V2_WRITE_TOOL, "arguments": call.arguments}


# --- sending -----------------------------------------------------------------------------


class FakeMcp:
    def __init__(self, results):
        self.results = list(results)
        self.sent = []

    def call_tool(self, name, arguments, *, is_write):
        self.sent.append((name, arguments, is_write))
        return self.results.pop(0)


OK = ToolResult(is_error=False, content=[{"type": "text", "text": '{"uuid": "v-1"}'}])
BAD = ToolResult(is_error=True, content=[{"type": "text", "text": "HTTP error 400"}])


def test_send_marks_every_call_as_a_write():
    mcp = FakeMcp([OK])
    calls = _render([op()])
    results = send(calls, mcp)
    assert mcp.sent == [(V2_WRITE_TOOL, calls[0].arguments, True)]
    assert results[0].ok and results[0].version_uuid == "v-1"


def test_send_stops_at_the_first_failure():
    mcp = FakeMcp([BAD, OK])
    calls = _render([op(), op(tool="update_feature_state", segment=None,
                              feature="agent_tool_policy", value="restricted")])
    results = send(calls, mcp)
    assert len(mcp.sent) == 1
    assert not results[0].ok and "400" in results[0].detail


def test_send_refuses_anything_not_rendered():
    with pytest.raises(WriterError):
        send([McpCall("delete_feature", {"id": 1})], FakeMcp([OK]))


# --- resolving ids through MCP reads ------------------------------------------------------


def test_mcp_resolver_reads_features_and_segments_once():
    pages = {
        "list_project_features": {"count": 1, "results": [{"id": 306127,
                                                           "name": "agent_prompt_version"}]},
        "list_project_segments": {"count": 1, "results": [{"id": 1205876,
                                                           "name": "tier0_production"}]},
    }

    class ReadMcp:
        calls = []

        def call_tool(self, name, arguments, *, is_write):
            assert is_write is False
            self.calls.append((name, arguments))
            return ToolResult(False, [{"type": "text", "text": json.dumps(pages[name])}])

    mcp = ReadMcp()
    ids = McpIdResolver(mcp, project_id=56664)
    assert ids.feature_id("agent_prompt_version") == 306127
    assert ids.segment_id("tier0_production") == 1205876
    assert ids.feature_id("agent_prompt_version") == 306127
    assert [c[0] for c in mcp.calls] == ["list_project_features", "list_project_segments"]
    assert mcp.calls[0][1]["project_pk"] == 56664
    with pytest.raises(WriterError):
        ids.feature_id("nope")


class PagedMcp:
    """Serves list reads from a table of pages per tool; a str page is sent as raw text."""

    def __init__(self, pages, *, is_error=False):
        self.pages = pages
        self.is_error = is_error
        self.calls = []

    def call_tool(self, name, arguments, *, is_write):
        assert is_write is False
        self.calls.append((name, dict(arguments)))
        page = self.pages[name][arguments.get("page", 1) - 1]
        text = page if isinstance(page, str) else json.dumps(page)
        return ToolResult(self.is_error, [{"type": "text", "text": text}])

    def names(self):
        return [c[0] for c in self.calls]


def _page(rows, *, more):
    return {"count": 0, "next": "https://api.example/next" if more else None,
            "previous": None, "results": rows}


def test_mcp_resolver_follows_pagination():
    mcp = PagedMcp({"list_project_features": [
        _page([{"id": 1, "name": "a"}], more=True),
        _page([{"id": 2, "name": "b"}], more=True),
        _page([{"id": 3, "name": "c"}], more=False),
    ]})
    ids = McpIdResolver(mcp, project_id=56664)
    assert ids.feature_id("c") == 3
    assert ids.feature_id("a") == 1
    pages = [c[1] for c in mcp.calls]
    assert [p.get("page", 1) for p in pages] == [1, 2, 3]
    assert all(p["page_size"] == 100 and p["project_pk"] == 56664 for p in pages)


def test_mcp_resolver_caps_runaway_pagination():
    endless = [_page([{"id": i, "name": f"f{i}"}], more=True) for i in range(1, 60)]
    mcp = PagedMcp({"list_project_features": endless})
    with pytest.raises(WriterError, match="pages"):
        McpIdResolver(mcp, project_id=56664).feature_id("f1")
    assert len(mcp.calls) <= 20


@pytest.mark.parametrize("page", [
    "not json at all",
    {"results": [{"id": 1}]},
    {"results": [{"name": "a"}]},
    {"results": ["a"]},
    {"results": [{"id": "1", "name": "a"}]},
    {"results": [{"id": True, "name": "a"}]},
])
def test_mcp_resolver_wraps_bad_payloads_as_writer_errors(page):
    mcp = PagedMcp({"list_project_features": [page]})
    with pytest.raises(WriterError):
        McpIdResolver(mcp, project_id=56664).feature_id("a")


def test_mcp_resolver_tool_error_is_a_writer_error():
    mcp = PagedMcp({"list_project_features": ["HTTP error 403"]}, is_error=True)
    with pytest.raises(WriterError, match="403"):
        McpIdResolver(mcp, project_id=56664).feature_id("a")


class GrowingMcp(PagedMcp):
    """A project whose features and segments change between reads."""

    def __init__(self):
        super().__init__({})
        self.features = [{"id": 1, "name": "a"}]
        self.segments = [{"id": 10, "name": "s"}]

    def call_tool(self, name, arguments, *, is_write):
        rows = self.features if name == "list_project_features" else self.segments
        self.pages[name] = [_page(list(rows), more=False)]
        return super().call_tool(name, arguments, is_write=is_write)


def test_mcp_resolver_reloads_features_once_on_a_miss():
    mcp = GrowingMcp()
    ids = McpIdResolver(mcp, project_id=56664)
    assert ids.feature_id("a") == 1
    mcp.features.append({"id": 2, "name": "new_flag"})
    assert ids.feature_id("new_flag") == 2
    assert mcp.names() == ["list_project_features"] * 2
    with pytest.raises(WriterError, match="unknown feature"):
        ids.feature_id("never_exists")
    assert mcp.names() == ["list_project_features"] * 3
    assert ids.feature_id("new_flag") == 2  # cached, no new read
    assert len(mcp.calls) == 3


def test_mcp_resolver_reloads_segments_once_on_a_miss():
    mcp = GrowingMcp()
    ids = McpIdResolver(mcp, project_id=56664)
    assert ids.segment_id("s") == 10
    mcp.segments.append({"id": 11, "name": "new_segment"})
    assert ids.segment_id("new_segment") == 11
    with pytest.raises(WriterError, match="unknown segment"):
        ids.segment_id("never_exists")
    assert mcp.names().count("list_project_segments") == 3
    assert mcp.names().count("list_project_features") <= 1
