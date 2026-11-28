import json

import httpx
import pytest

from agent.llm import (
    Completion,
    DisabledLLM,
    FakeLLM,
    GeminiLLM,
    LLMUnavailable,
    OllamaLLM,
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


# --- Ollama (local models, no key) ------------------------------------------------------------


def _ollama(handler, url="http://ollama:11434"):
    return OllamaLLM(url, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_ollama_request_shape_and_reply_parsing():
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content), dict(request.headers)))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "ok"},
                                         "prompt_eval_count": 12, "eval_count": 3})

    out = _ollama(handler).complete("rank these", model="llama3.2:3b", max_tokens=200)
    assert out == Completion(text="ok", tokens_in=12, tokens_out=3, model="llama3.2:3b")
    url, body, headers = seen[0]
    assert url == "http://ollama:11434/api/chat"
    assert body["model"] == "llama3.2:3b" and body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "rank these"}]
    assert body["options"] == {"num_predict": 200, "temperature": 0}
    assert "think" not in body  # llama doesn't think
    assert "authorization" not in {k.lower() for k in headers}  # no key, ever


def test_ollama_turns_thinking_off_for_qwen3():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "x"}})

    _ollama(handler).complete("p", model="qwen3:14b", max_tokens=5)
    assert bodies[0]["think"] is False


@pytest.mark.parametrize("status", [500, 502, 503, 404, 400])
def test_ollama_http_errors_are_unavailable(status):
    with pytest.raises(LLMUnavailable, match=f"HTTP {status}"):
        _ollama(lambda r: httpx.Response(status, text="model 'x' not found")).complete(
            "p", model="x", max_tokens=5)


def test_ollama_down_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailable, match="unreachable: ConnectError"):
        _ollama(handler).complete("p", model="m", max_tokens=5)


@pytest.mark.parametrize("body", [{"message": {}}, {"done": True}, {"message": {"content": 3}}])
def test_ollama_unexpected_bodies_are_unavailable(body):
    with pytest.raises(LLMUnavailable, match="unexpected body"):
        _ollama(lambda r: httpx.Response(200, json=body)).complete("p", model="m", max_tokens=5)


def test_llm_from_env_picks_ollama_without_a_key():
    llm = llm_from_env({"LLM_PROVIDER": "ollama"})
    assert isinstance(llm, OllamaLLM) and llm.url == "http://localhost:11434"
    llm = llm_from_env({"LLM_PROVIDER": " Ollama ",
                        "OLLAMA_URL": "http://host.docker.internal:11434/"})
    assert llm.url == "http://host.docker.internal:11434"
    assert isinstance(llm_from_env({"LLM_PROVIDER": "ollama", "LLM_LIVE": "0"}), DisabledLLM)


def test_ollama_quota_like_failure_falls_back_in_ordering():
    from agent.ordering import order_suspects
    from contracts.models import FlagChange, InvestigationMode
    suspects = [FlagChange(flag="a", old=1, new=2), FlagChange(flag="b", old=1, new=2)]
    llm = _ollama(lambda r: httpx.Response(503, text="busy"))
    out = order_suspects(suspects, None, InvestigationMode.GUIDED, llm, model="llama3.2:3b")
    assert out.source == "diff" and out.suspects == suspects and "unavailable" in out.note


def test_investigator_model_prefers_its_own_setting_then_the_flagship():
    from agent.llm import investigator_model
    assert investigator_model({"INVESTIGATOR_MODEL": "x", "MODEL_FLAGSHIP": "f",
                               "MODEL_CHEAP": "c"}) == "x"
    assert investigator_model({"MODEL_FLAGSHIP": "f", "MODEL_CHEAP": "c"}) == "f"
    assert investigator_model({"MODEL_CHEAP": "c"}) == "c"
    assert investigator_model({}) == ""


def test_wiring_gives_the_orchestrator_the_flagship_for_ranking():
    from orchestrator import wiring
    stores = [object()]
    orch = wiring.orchestrator_for(stores[0], {"MODEL_CHEAP": "qwen3:8b",
                                               "MODEL_FLAGSHIP": "qwen3:14b"})
    assert (orch.llm_model, orch.ordering_model) == ("qwen3:8b", "qwen3:14b")


def test_ranking_client_thinks_by_default_and_explanations_do_not():
    from agent.ordering import THINKING_MAX_TOKENS
    from orchestrator import wiring
    env = {"LLM_PROVIDER": "ollama", "MODEL_CHEAP": "qwen3:8b", "MODEL_FLAGSHIP": "qwen3:14b"}
    client = wiring.ranking_client(env)
    assert isinstance(client["ordering_llm"], OllamaLLM) and client["ordering_llm"].think is True
    assert client["ordering_max_tokens"] == THINKING_MAX_TOKENS == 2048
    assert llm_from_env(env).think is False  # explanations: thinking off
    assert wiring.ranking_client({**env, "INVESTIGATOR_THINK": "0"}) == {}
    stores = [object()]
    orch = wiring.orchestrator_for(stores[0], env)
    assert orch.ordering_llm.think is True and orch.llm.think is False
    assert (orch.ordering_model, orch.ordering_max_tokens) == ("qwen3:14b", 2048)
