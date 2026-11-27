import json

import httpx
import pytest

from agent.llm import DisabledLLM, LLMUnavailable, OllamaLLM, OpenAICompatLLM, llm_from_env


def _llm(handler, key="", **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatLLM("http://ollama.test:11434/v1/", key, client=client, **kw)


def _reply(content, prompt_tokens=7, completion_tokens=3):
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})


def test_request_shape_without_a_key_has_no_auth_header():
    seen = {}

    def handler(req):
        seen["url"], seen["headers"] = str(req.url), req.headers
        seen["body"] = json.loads(req.content)
        return _reply("agent_prompt_version first")

    out = _llm(handler).complete("order these", model="qwen2.5:14b", max_tokens=64)
    assert seen["url"] == "http://ollama.test:11434/v1/chat/completions"
    assert "authorization" not in seen["headers"]
    assert seen["body"] == {"model": "qwen2.5:14b", "max_tokens": 64, "temperature": 0,
                            "messages": [{"role": "user", "content": "order these"}]}
    assert (out.text, out.tokens_in, out.tokens_out, out.model) == \
        ("agent_prompt_version first", 7, 3, "qwen2.5:14b")


def test_bearer_key_and_reasoning_effort_are_sent():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return _reply("ok")

    _llm(handler, "gsk_test", reasoning_effort="none").complete("p", model="m", max_tokens=5)
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["body"]["reasoning_effort"] == "none"


def test_think_blocks_are_stripped():
    out = _llm(lambda r: _reply("<think>\nhmm, the tools\n</think>\n\nThe prompt flag."))\
        .complete("p", model="m", max_tokens=5)
    assert out.text == "The prompt flag."


@pytest.mark.parametrize(("status", "reason"), [(429, "quota or rate limit"), (500, "error")])
def test_http_errors_are_unavailable_without_the_key(status, reason):
    llm = _llm(lambda r: httpx.Response(status, text="nope"), "gsk_secret")
    with pytest.raises(LLMUnavailable, match=reason) as exc:
        llm.complete("p", model="m", max_tokens=5)
    assert "gsk_secret" not in str(exc.value) and "gsk_secret" not in repr(llm)


def test_network_error_and_bad_body_are_unavailable():
    def down(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMUnavailable, match="unreachable"):
        _llm(down).complete("p", model="m", max_tokens=5)
    with pytest.raises(LLMUnavailable, match="unexpected body"):
        _llm(lambda r: httpx.Response(200, json={"choices": []})).complete(
            "p", model="m", max_tokens=5)


def test_null_content_is_empty_text():
    assert _llm(lambda r: _reply(None)).complete("p", model="m", max_tokens=5).text == ""


def test_from_env_ollama_is_native_with_alias_and_v1_stripped():
    llm = llm_from_env({"LLM_PROVIDER": "ollama"})
    assert isinstance(llm, OllamaLLM) and llm.think is False
    assert llm.url == "http://localhost:11434/api/chat"
    docker = llm_from_env({"LLM_PROVIDER": "Ollama",
                           "LLM_BASE_URL": "http://host.docker.internal:11434/v1"})
    assert docker.url == "http://host.docker.internal:11434/api/chat"
    alias = llm_from_env({"LLM_PROVIDER": "ollama", "OLLAMA_URL": "http://mac.local:11434/",
                          "LLM_THINK": "1"})
    assert alias.url == "http://mac.local:11434/api/chat" and alias.think is True


def test_ollama_native_request_and_reply():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "<think>x</think> tools first"},
            "prompt_eval_count": 11, "eval_count": 4})

    llm = OllamaLLM("http://ollama.test:11434",
                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = llm.complete("order", model="llama3.2:3b", max_tokens=200)
    assert seen["url"] == "http://ollama.test:11434/api/chat"
    assert seen["body"] == {"model": "llama3.2:3b", "stream": False, "think": False,
                            "messages": [{"role": "user", "content": "order"}],
                            "options": {"temperature": 0, "num_predict": 200}}
    assert (out.text, out.tokens_in, out.tokens_out) == ("tools first", 11, 4)


def test_ollama_missing_model_says_to_pull():
    llm = OllamaLLM(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"error": "model 'qwen3:14b' not found"}))))
    with pytest.raises(LLMUnavailable, match="ollama pull"):
        llm.complete("p", model="qwen3:14b", max_tokens=5)


def test_from_env_groq_needs_a_key_and_openai_needs_a_base():
    assert isinstance(llm_from_env({"LLM_PROVIDER": "groq"}), DisabledLLM)
    groq = llm_from_env({"LLM_PROVIDER": "groq", "LLM_API_KEY": "gsk_x"})
    assert groq.url == "https://api.groq.com/openai/v1/chat/completions"
    assert isinstance(llm_from_env({"LLM_PROVIDER": "openai"}), DisabledLLM)


def test_llm_live_zero_still_wins():
    assert isinstance(llm_from_env({"LLM_LIVE": "0", "LLM_PROVIDER": "ollama"}), DisabledLLM)
