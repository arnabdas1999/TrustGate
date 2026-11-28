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
