"""Ollama (native /api/chat) and OpenAI-compatible (Groq, ...) wire formats, and provider_from_env.
No network: every request goes to an httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from sandbox import llm, runner

SPEC = llm.ToolSpec("get_cluster_status", "Cluster health.", {
    "type": "object", "properties": {"cluster": {"type": "string"}}, "required": ["cluster"],
    "additionalProperties": False})


def _mock(responses):
    """A client that answers with (status, body) pairs in order and records each request."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _openai(responses, key=""):
    client, sent = _mock(responses)
    provider = llm.OpenAICompatProvider("https://api.example.test/v1/", key, client=client,
                                        sleep=lambda s: None)
    return provider, sent


def _ollama(responses, base="http://ollama.test:11434", **kw):
    client, sent = _mock(responses)
    return llm.OllamaProvider(base, client=client, sleep=lambda s: None, **kw), sent


def _completion(content=None, tool_calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _ollama_reply(content="", tool_calls=None, tokens=(0, 0)):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": "m", "message": message, "done": True, "done_reason": "stop",
            "prompt_eval_count": tokens[0], "eval_count": tokens[1]}


# -- OpenAI-compatible ---------------------------------------------------------------------------


def test_openai_request_shape_with_tools_and_seed():
    provider, sent = _openai([(200, _completion("hi"))], key="gsk_secret")
    chat = provider.open_chat(model="m1", system="sys", user="hello", tools=[SPEC],
                              temperature=0.2, seed=7)
    assert chat.send(None).text == "hi"
    (request,) = sent
    assert str(request.url) == "https://api.example.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer gsk_secret"
    body = _body(request)
    assert body["model"] == "m1" and body["temperature"] == 0.2 and body["seed"] == 7
    assert body["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "hello"}]
    assert body["tools"] == [{"type": "function", "function": {
        "name": "get_cluster_status", "description": "Cluster health.",
        "parameters": SPEC.parameters}}]
    assert body["tool_choice"] == "auto"


def test_openai_no_tools_no_seed_no_key():
    provider, sent = _openai([(200, _completion("x"))])
    provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0).send(None)
    body = _body(sent[0])
    assert "tools" not in body and "tool_choice" not in body and "seed" not in body
    assert "authorization" not in sent[0].headers


def test_openai_tool_call_round_trip_through_loop():
    call = {"id": "call_abc", "type": "function",
            "function": {"name": "get_cluster_status", "arguments": '{"cluster": "a"}'}}
    provider, sent = _openai([
        (200, _completion(None, [call], {"prompt_tokens": 50, "completion_tokens": 5})),
        (200, _completion("<think>\nall good?\n</think>\n\nCluster a is healthy.",
                          usage={"prompt_tokens": 70, "completion_tokens": 9})),
    ])
    chat = provider.open_chat(model="m", system="sys", user="hi", tools=[SPEC], temperature=0)
    seen = []
    result = llm.run_tool_loop(chat, lambda n, a: seen.append((n, a)) or {"ok": True},
                               max_steps=4)
    assert seen == [("get_cluster_status", {"cluster": "a"})]
    assert [c.name for c in result.tool_calls] == ["get_cluster_status"]
    assert result.final_text == "Cluster a is healthy." and not result.hit_step_limit
    assert result.tokens_in == 120 and result.tokens_out == 14 and result.steps == 2
    messages = _body(sent[1])["messages"]
    assert messages[2] == {"role": "assistant", "content": "", "tool_calls": [call]}
    assert messages[3] == {"role": "tool", "tool_call_id": "call_abc",
                           "content": json.dumps({"ok": True})}


def test_openai_missing_ids_get_stable_ids_and_dict_arguments():
    calls = [{"function": {"name": "a", "arguments": {"x": 1}}},
             {"function": {"name": "b", "arguments": ""}}]
    provider, sent = _openai([(200, _completion("", calls)), (200, _completion("done"))])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[SPEC], temperature=0)
    turn = chat.send(None)
    assert [(c.name, c.args, c.id) for c in turn.calls] == [("a", {"x": 1}, "call_1"),
                                                            ("b", {}, "call_2")]
    chat.send([(c, {"ok": True}) for c in turn.calls])
    messages = _body(sent[1])["messages"]
    assert [tc["id"] for tc in messages[2]["tool_calls"]] == ["call_1", "call_2"]
    assert [m["tool_call_id"] for m in messages[3:]] == ["call_1", "call_2"]


def test_openai_bad_json_arguments_is_llm_error():
    calls = [{"id": "c", "function": {"name": "a", "arguments": "{not json"}}]
    provider, _ = _openai([(200, _completion(None, calls))])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[SPEC], temperature=0)
    with pytest.raises(llm.LLMError, match="not valid JSON"):
        chat.send(None)


def test_think_stripping():
    assert llm._strip_think("<think>a\nb</think>answer") == "answer"
    assert llm._strip_think("before <think>x</think> after") == "before  after"
    assert llm._strip_think("<think>cut off mid-thought") == ""
    assert llm._strip_think("plain") == "plain"


def test_openai_missing_usage_is_zero_tokens():
    provider, _ = _openai([(200, _completion("x"))])
    turn = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0).send(None)
    assert turn.tokens_in == 0 and turn.tokens_out == 0


def test_openai_retries_429_then_succeeds():
    slept = []
    client, sent = _mock([(429, {"error": {"message": "rate limited"}}),
                          (503, {"error": "busy"}), (200, _completion("x"))])
    provider = llm.OpenAICompatProvider("https://api.example.test/v1", "k", client=client,
                                        sleep=slept.append)
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    assert chat.send(None).text == "x" and len(sent) == 3 and len(slept) == 2


def test_openai_transport_error_retried_then_reported():
    def handler(request):
        raise httpx.ConnectError("refused")

    provider = llm.OpenAICompatProvider(
        "https://api.example.test/v1", "k", retries=2, sleep=lambda s: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    with pytest.raises(llm.LLMError, match="unreachable: ConnectError"):
        chat.send(None)


def test_openai_client_error_text_without_key():
    provider, _ = _openai([(400, {"error": {"message": "bad tool schema for sk-SECRET"}})],
                          key="sk-SECRET")
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    with pytest.raises(llm.LLMError) as err:
        chat.send(None)
    assert "400" in str(err.value) and "bad tool schema" in str(err.value)
    assert "sk-SECRET" not in str(err.value) and "sk-SECRET" not in repr(provider)


def test_error_text_shapes():
    long = "x" * 1000
    assert llm._error_text(httpx.Response(400, json={"error": {"message": "m"}})) == "m"
    assert llm._error_text(httpx.Response(400, json={"error": "plain"})) == "plain"
    assert len(llm._error_text(httpx.Response(400, json={"error": long}))) == 300
    assert llm._error_text(httpx.Response(502, text="<html>gateway</html>")) == \
        "<html>gateway</html>"


# -- Ollama native -------------------------------------------------------------------------------


def test_ollama_native_request_shape():
    provider, sent = _ollama([(200, _ollama_reply("hi", tokens=(11, 2)))],
                             base="http://ollama.test:11434/v1/")
    chat = provider.open_chat(model="qwen3:14b", system="sys", user="hello", tools=[SPEC],
                              temperature=0.0, seed=5)
    turn = chat.send(None)
    assert turn.text == "hi" and (turn.tokens_in, turn.tokens_out) == (11, 2)
    (request,) = sent
    assert str(request.url) == "http://ollama.test:11434/api/chat"  # /v1 stripped
    assert "authorization" not in request.headers
    body = _body(request)
    assert body["model"] == "qwen3:14b" and body["stream"] is False and body["think"] is False
    assert body["options"] == {"temperature": 0.0, "seed": 5}
    assert body["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "hello"}]
    assert body["tools"][0]["function"]["name"] == "get_cluster_status"
    assert "tool_choice" not in body


def test_ollama_no_tools_no_seed_think_on():
    provider, sent = _ollama([(200, _ollama_reply("<think>hmm</think>ok"))], think=True)
    turn = provider.open_chat(model="m", system="s", user="u", tools=[],
                              temperature=0.3).send(None)
    body = _body(sent[0])
    assert turn.text == "ok"
    assert "tools" not in body and body["options"] == {"temperature": 0.3}
    assert body["think"] is True


def test_ollama_tool_call_round_trip_through_loop():
    call = {"function": {"name": "get_cluster_status", "arguments": {"cluster": "a"}}}
    provider, sent = _ollama([
        (200, _ollama_reply("", [call], tokens=(40, 6))),
        (200, _ollama_reply("<think>\n</think>\n\nCluster a is healthy.", tokens=(60, 8))),
    ])
    chat = provider.open_chat(model="llama3.2:3b", system="sys", user="hi", tools=[SPEC],
                              temperature=0, seed=1)
    result = llm.run_tool_loop(chat, lambda n, a: {"ok": True, "health": "healthy"},
                               max_steps=4)
    assert [(c.name, c.args) for c in result.tool_calls] == [
        ("get_cluster_status", {"cluster": "a"})]
    assert result.final_text == "Cluster a is healthy."
    assert result.tokens_in == 100 and result.tokens_out == 14 and result.steps == 2
    messages = _body(sent[1])["messages"]
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["function"] == call["function"]
    assert messages[3] == {"role": "tool", "tool_name": "get_cluster_status",
                           "content": json.dumps({"ok": True, "health": "healthy"})}


def test_ollama_missing_model_says_pull():
    provider, sent = _ollama([(404, {"error": "model \"qwen3:14b\" not found, try pulling it"})])
    chat = provider.open_chat(model="qwen3:14b", system="s", user="u", tools=[], temperature=0)
    with pytest.raises(llm.LLMError, match=r"ollama pull qwen3:14b"):
        chat.send(None)
    assert len(sent) == 1  # a 404 is not retried


def test_ollama_retries_503_then_succeeds():
    provider, sent = _ollama([(503, {"error": "server busy"}), (200, _ollama_reply("x"))])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    assert chat.send(None).text == "x" and len(sent) == 2


def test_ollama_error_in_200_body():
    provider, _ = _ollama([(200, {"error": "out of memory"})])
    chat = provider.open_chat(model="m", system="s", user="u", tools=[], temperature=0)
    with pytest.raises(llm.LLMError, match="out of memory"):
        chat.send(None)


# -- provider_from_env -------------------------------------------------------------------------


def test_env_gemini_is_still_the_default():
    provider = llm.provider_from_env({"LLM_API_KEY": "k"})
    assert isinstance(provider, llm.GeminiProvider)
    assert provider.client.timeout.read == 90.0


def test_env_ollama_defaults():
    provider = llm.provider_from_env({"LLM_PROVIDER": "ollama"})
    assert isinstance(provider, llm.OllamaProvider)
    assert provider.base_url == "http://localhost:11434" and provider.think is False
    assert provider.client.timeout.read == 300.0
    assert "authorization" not in provider.client.headers


def test_env_ollama_url_alias_base_override_and_think():
    env = {"LLM_PROVIDER": " Ollama ", "OLLAMA_URL": "http://mac.local:11434/v1"}
    assert llm.provider_from_env(env).base_url == "http://mac.local:11434"
    env["LLM_BASE_URL"] = "http://host.docker.internal:11434/"
    assert llm.provider_from_env(env).base_url == "http://host.docker.internal:11434"
    env.update(LLM_THINK="1", LLM_TIMEOUT_S="42.5")
    provider = llm.provider_from_env(env)
    assert provider.think is True and provider.client.timeout.read == 42.5


def test_env_groq():
    provider = llm.provider_from_env({"LLM_PROVIDER": "groq", "LLM_API_KEY": "gsk_k"})
    assert isinstance(provider, llm.OpenAICompatProvider)
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert provider.client.timeout.read == 90.0
    with pytest.raises(llm.LLMError, match="LLM_API_KEY"):
        llm.provider_from_env({"LLM_PROVIDER": "groq"})
    override = llm.provider_from_env({"LLM_PROVIDER": "groq", "LLM_API_KEY": "k",
                                      "LLM_BASE_URL": "https://proxy.test/v1"})
    assert override.base_url == "https://proxy.test/v1"


def test_env_openai_needs_base_url():
    with pytest.raises(llm.LLMError, match="LLM_BASE_URL"):
        llm.provider_from_env({"LLM_PROVIDER": "openai", "LLM_API_KEY": "k"})
    provider = llm.provider_from_env({"LLM_PROVIDER": "openai",
                                      "LLM_BASE_URL": "http://vllm.test:8000/v1"})
    assert provider.base_url == "http://vllm.test:8000/v1"


def test_env_bad_timeout_and_unknown_provider():
    with pytest.raises(llm.LLMError, match="LLM_TIMEOUT_S"):
        llm.provider_from_env({"LLM_PROVIDER": "ollama", "LLM_TIMEOUT_S": "slow"})
    with pytest.raises(llm.LLMError) as err:
        llm.provider_from_env({"LLM_PROVIDER": "anthropic"})
    assert all(name in str(err.value) for name in ("gemini", "ollama", "groq", "openai"))


def test_repr_hides_key():
    for provider in (llm.provider_from_env({"LLM_PROVIDER": "groq", "LLM_API_KEY": "gsk_SECRET"}),
                     llm.provider_from_env({"LLM_PROVIDER": "ollama", "LLM_API_KEY": "gsk_SECRET"}),
                     llm.provider_from_env({"LLM_API_KEY": "gsk_SECRET"})):
        assert "gsk_SECRET" not in repr(provider)


def test_local_model_limits(monkeypatch):
    assert llm.run_timeout_s({}) == 120.0
    assert llm.run_timeout_s({"LLM_PROVIDER": "ollama"}) == 900.0
    assert llm.run_timeout_s({"LLM_PROVIDER": "ollama", "SANDBOX_RUN_TIMEOUT_S": "60"}) == 60.0
    monkeypatch.delenv("SANDBOX_PARALLEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert runner.default_parallelism() == 2
    monkeypatch.setenv("SANDBOX_PARALLEL", "4")
    assert runner.default_parallelism() == 4
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("SANDBOX_PARALLEL")
    assert runner.default_parallelism() == 8
