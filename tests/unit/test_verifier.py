import httpx
import pytest

from contracts.models import Identity
from verifier.readback import (
    EdgeFlagSource,
    Expectation,
    ServedFlag,
    SourceUnavailable,
    VerifyStatus,
    verify,
)

TIER0 = Identity(identifier="payments-prod", traits={"env": "prod", "tier": 0})
STAGING = Identity(identifier="staging-api", traits={"env": "staging", "tier": 2})
EXPECT = [Expectation(TIER0, {"agent_prompt_version": "v1_strict"}),
          Expectation(STAGING, {"agent_prompt_version": "v2_fast"})]


def served(value, is_default=False):
    return {"agent_prompt_version": ServedFlag(value=value, enabled=True, is_default=is_default)}


class FakeSource:
    """Returns one scripted answer per identity per poll."""

    def __init__(self, polls, per_poll=2):
        self.polls = list(polls)
        self.per_poll = per_poll  # identities asked per poll
        self.calls = 0

    def identity_flags(self, identity):
        poll = self.polls[min(self.calls // self.per_poll, len(self.polls) - 1)]
        self.calls += 1
        answer = poll[identity.identifier]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _verify(source, timeout=60):
    c = Clock()
    return verify(EXPECT, source, timeout_s=timeout, interval_s=5, clock=c.now, sleep=c.sleep)


def test_verified_when_every_identity_gets_its_value():
    src = FakeSource([{"payments-prod": served("v1_strict"), "staging-api": served("v2_fast")}])
    result = _verify(src)
    assert result.status is VerifyStatus.VERIFIED
    assert [r.values for r in result.readbacks] == [{"agent_prompt_version": "v1_strict"},
                                                    {"agent_prompt_version": "v2_fast"}]
    assert not any(r.from_default_handler for r in result.readbacks)


def test_polls_until_the_new_value_is_served():
    old = {"payments-prod": served("v2_fast"), "staging-api": served("v2_fast")}
    new = {"payments-prod": served("v1_strict"), "staging-api": served("v2_fast")}
    result = _verify(FakeSource([old, old, new]))
    assert result.status is VerifyStatus.VERIFIED
    assert result.elapsed_s == 10


def test_wrong_value_until_timeout_is_failed():
    stuck = {"payments-prod": served("v2_fast"), "staging-api": served("v2_fast")}
    result = _verify(FakeSource([stuck]), timeout=20)
    assert result.status is VerifyStatus.FAILED
    assert "payments-prod" in result.detail


def test_default_handler_values_never_count():
    # The right value, but served by the default handler: Flagsmith wasn't reached.
    fake = {"payments-prod": served("v1_strict", is_default=True),
            "staging-api": served("v2_fast")}
    result = _verify(FakeSource([fake]), timeout=20)
    assert result.status is VerifyStatus.TIMEOUT
    assert result.readbacks[0].from_default_handler


def test_unreachable_flagsmith_is_a_timeout():
    down = {"payments-prod": SourceUnavailable("edge down"),
            "staging-api": SourceUnavailable("edge down")}
    result = _verify(FakeSource([down]), timeout=10)
    assert result.status is VerifyStatus.TIMEOUT


def test_missing_flag_counts_as_default():
    src = FakeSource([{"payments-prod": {}, "staging-api": served("v2_fast")}])
    assert _verify(src, timeout=10).status is VerifyStatus.TIMEOUT


@pytest.mark.parametrize(("served_value", "expected", "ok"), [
    ("6", 6, True), (6, "6", True), ("0.0", "0.0", True), (False, False, True),
    (False, "false", False), ("v1", "v2", False),
    ("0.0", 0.0, True), (0.0, "0.0", True), ("0.5", 0.5, True), (0.5, "0.5", True),
    (0.5, 0.5, True), ("0.50", 0.5, True), ("0.5", 0.6, False), ("abc", 0.5, False),
    (6, 6.0, True), ("6", 6.0, True), (True, 1.0, False), (0.0, False, False),
])
def test_value_comparison(served_value, expected, ok):
    exp = [Expectation(TIER0, {"f": expected})]
    src = FakeSource([{"payments-prod": {"f": ServedFlag(served_value, True, False)}}],
                     per_poll=1)
    c = Clock()
    status = verify(exp, src, timeout_s=0, interval_s=1, clock=c.now, sleep=c.sleep).status
    assert (status is VerifyStatus.VERIFIED) is ok


def test_needs_at_least_one_expectation():
    with pytest.raises(ValueError):
        verify([], FakeSource([{}]))


# --- edge source ---------------------------------------------------------------------------


def test_edge_source_sends_transient_identity_with_traits():
    seen = {}

    def handler(request):
        import json
        seen["key"] = request.headers["X-Environment-Key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"flags": [
            {"feature": {"name": "agent_prompt_version"}, "enabled": True,
             "feature_state_value": "v1_strict"}], "traits": []})

    src = EdgeFlagSource("ser.k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    flags = src.identity_flags(TIER0)
    assert flags["agent_prompt_version"] == ServedFlag("v1_strict", True, False)
    assert seen["key"] == "ser.k"
    assert seen["body"]["transient"] is True
    assert {"trait_key": "tier", "trait_value": 0} in seen["body"]["traits"]


def test_edge_source_errors_are_unavailable_and_hide_the_key():
    def handler(request):
        return httpx.Response(500, text="ser.k broke")

    src = EdgeFlagSource("ser.k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SourceUnavailable) as err:
        src.identity_flags(TIER0)
    assert "ser.k" not in str(err.value)


def test_edge_source_needs_a_server_key():
    with pytest.raises(ValueError):
        EdgeFlagSource("client-key")
