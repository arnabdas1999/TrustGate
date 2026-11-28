"""The scaling test's guided arm ranks with the same investigator model as the orchestrator."""
import pytest

from agent.llm import FakeLLM


class ModelRecordingLLM(FakeLLM):
    def __init__(self, answers):
        super().__init__(answers)
        self.models = []

    def complete(self, prompt, *, model, max_tokens):
        self.models.append(model)
        return super().complete(prompt, model=model, max_tokens=max_tokens)


def test_scaling_guided_order_uses_the_investigator_model():
    from search.scaling import build_release, guided_order
    _, suspects = build_release(5, 0)
    llm = ModelRecordingLLM(['["agent_prompt_version"]'])
    guided_order(suspects, llm=llm, env={"MODEL_CHEAP": "qwen3:8b",
                                         "MODEL_FLAGSHIP": "qwen3:14b"})
    assert llm.models == ["qwen3:14b"]
    llm2 = ModelRecordingLLM(['["agent_prompt_version"]'])
    guided_order(suspects, llm=llm2, env={"INVESTIGATOR_MODEL": "inv", "MODEL_FLAGSHIP": "f"})
    assert llm2.models == ["inv"]


def test_scaling_guided_order_needs_a_model():
    from search.scaling import build_release, guided_order
    _, suspects = build_release(5, 0)
    with pytest.raises(ValueError, match="investigator model"):
        guided_order(suspects, llm=FakeLLM(["[]"]), env={})


class BudgetRecordingLLM(ModelRecordingLLM):
    def __init__(self, answers):
        super().__init__(answers)
        self.max_tokens = []

    def complete(self, prompt, *, model, max_tokens):
        self.max_tokens.append(max_tokens)
        return super().complete(prompt, model=model, max_tokens=max_tokens)


def test_scaling_guided_order_uses_the_thinking_budget_and_client():
    from agent.ordering import MAX_TOKENS, THINKING_MAX_TOKENS
    from search.scaling import build_release, guided_order
    _, suspects = build_release(5, 0)
    env = {"MODEL_FLAGSHIP": "qwen3:14b", "LLM_PROVIDER": "ollama"}
    llm = BudgetRecordingLLM(['["agent_prompt_version"]'])
    guided_order(suspects, llm=llm, env=env)
    assert llm.max_tokens == [THINKING_MAX_TOKENS]
    off = BudgetRecordingLLM(['["agent_prompt_version"]'])
    guided_order(suspects, llm=off, env={**env, "INVESTIGATOR_THINK": "0"})
    assert off.max_tokens == [MAX_TOKENS]


def test_scaling_builds_the_thinking_client_when_none_is_given(monkeypatch):
    import agent.ordering
    from agent.llm import OllamaLLM
    from search.scaling import build_release, guided_order
    _, suspects = build_release(5, 0)
    seen = []

    def fake_order(suspects, trace, mode, llm, *, model, seed="", max_tokens=0):
        seen.append((type(llm).__name__, getattr(llm, "think", None), model, max_tokens))
        return agent.ordering.Ordering(list(suspects), "llm")
    monkeypatch.setattr(agent.ordering, "order_suspects", fake_order)
    guided_order(suspects, env={"MODEL_FLAGSHIP": "qwen3:14b", "LLM_PROVIDER": "ollama"})
    assert seen == [(OllamaLLM.__name__, True, "qwen3:14b", 2048)]
