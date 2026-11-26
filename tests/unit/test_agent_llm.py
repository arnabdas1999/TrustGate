import json

import httpx
import pytest

from agent.llm import (
    Completion,
    DisabledLLM,
    FakeLLM,
    GeminiLLM,
    LLMUnavailable,
    llm_from_env,
    model_id,
)


def test_fake_llm_replays_scripted_answers_and_counts_tokens():
    llm = FakeLLM(["first", "second"])
    a = llm.complete("hello world", model="m", max_tokens=10)
    assert a == Completion(text="first", tokens_in=2, tokens_out=1, model="m")
    assert llm.complete("x", model="m", max_tokens=10).text == "second"
    assert llm.prompts == ["hello world", "x"]


def test_fake_llm_can_raise_to_simulate_quota():
    with pytest.raises(LLMUnavailable):
        FakeLLM([LLMUnavailable("quota")]).complete("p", model="m", max_tokens=5)


def test_disabled_llm_never_calls_anything():
    with pytest.raises(LLMUnavailable, match="LLM_LIVE=0"):
        DisabledLLM("LLM_LIVE=0").complete("p", model="m", max_tokens=5)


def _gemini(handler):
    return GeminiLLM("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_gemini_request_shape_and_reply_parsing():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "ranked"}, {"text": " list"}]}}],
            "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 3}})

    out = _gemini(handler).complete("prompt text", model="gemini-x", max_tokens=64)
    assert out == Completion(text="ranked list", tokens_in=40, tokens_out=3, model="gemini-x")
    assert seen["url"].endswith("/models/gemini-x:generateContent")
    assert seen["key"] == "test-key"
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "prompt text"
    assert seen["body"]["generationConfig"] == {"maxOutputTokens": 64, "temperature": 0}


@pytest.mark.parametrize("status", [402, 429])
def test_gemini_quota_and_payment_errors_are_unavailable(status):
    with pytest.raises(LLMUnavailable, match=str(status)):
        _gemini(lambda r: httpx.Response(status, text="quota exceeded")).complete(
            "p", model="m", max_tokens=5)


def test_gemini_quota_text_in_another_error_is_unavailable():
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
    with pytest.raises(LLMUnavailable):
        _gemini(lambda r: httpx.Response(400, json=body)).complete("p", model="m", max_tokens=5)


def test_gemini_network_error_is_unavailable_and_hides_the_key():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(LLMUnavailable) as err:
        _gemini(handler).complete("p", model="m", max_tokens=5)
    assert "test-key" not in str(err.value)


def test_gemini_needs_a_key():
    with pytest.raises(ValueError):
        GeminiLLM("")


def test_llm_from_env():
    assert isinstance(llm_from_env({}), DisabledLLM)
    assert isinstance(llm_from_env({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "k",
                                    "LLM_LIVE": "0"}), DisabledLLM)
    assert isinstance(llm_from_env({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "k"}), GeminiLLM)
    assert isinstance(llm_from_env({"LLM_PROVIDER": "openai", "LLM_API_KEY": "k"}), DisabledLLM)


def test_model_id_maps_flag_values_to_env():
    env = {"MODEL_CHEAP": "gemini-small", "MODEL_FLAGSHIP": "gemini-big"}
    assert model_id("model_cheap", env) == "gemini-small"
    assert model_id("model_flagship", env) == "gemini-big"
    with pytest.raises(ValueError):
        model_id("model_cheap", {})


@pytest.mark.llm
def test_gemini_live_round_trip():
    import os
    llm = llm_from_env()
    if not isinstance(llm, GeminiLLM):
        pytest.skip("LLM_PROVIDER=gemini and LLM_API_KEY not set")
    out = llm.complete("Reply with the single word: ok", model=model_id("model_cheap"),
                       max_tokens=5)
    assert out.text.strip() and out.tokens_in > 0
    assert os.environ.get("LLM_API_KEY", "") not in out.text


def test_a_good_answer_that_mentions_quota_is_still_an_answer():
    body = {"candidates": [{"content": {"parts": [{"text": "raise the quota"}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 3}}
    out = _gemini(lambda r: httpx.Response(200, json=body)).complete("p", model="m",
                                                                     max_tokens=5)
    assert out.text == "raise the quota"
