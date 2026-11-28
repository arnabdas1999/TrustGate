# Guided ordering diagnostics (Nov 28)

Context: eval run 2 (opspilot-eval-1 / guided) ran exactly like plain. The suspects were
tested in diff order, with the same ddmin path, the same 59 runs and the same 5 diagnostic
runs. The record is kept as it is.

**The first three diagnostics below were run on the EVAL case opspilot-eval-1** (its captured
incident), before Arnab's condition that the ranking check must use a dev case. Their outputs
are kept here unchanged. All further ranking diagnostics use the dev case opspilot-dev-1.

Each diagnostic is one LLM call through main's `agent/llm.py` (OllamaLLM on the host), with
the orchestrator's `agent.ordering` prompt and its strict `_parse`. No API, no Flagsmith, no
records. Naman OK'd them in chat.

## 1. qwen3:8b (MODEL_CHEAP, the investigator in eval runs 1–2), think off, diff order shown

- Shown (prompt) order: the diff order, alphabetical: `agent_context_mode, agent_max_steps,
  agent_model, agent_prompt_version, agent_tool_policy`
- Raw reply: `["agent_context_mode", "agent_max_steps", "agent_model", "agent_prompt_version",
  "agent_tool_policy"]`. 659 tokens in, 25 out.
- Parsed cleanly, so Ordering.source = "llm". **An exact echo of the input order.** Guided
  therefore equalled plain in eval run 2 (finding).

## 2. qwen3:14b (MODEL_FLAGSHIP), think off, diff order shown

- Raw reply: `["agent_tool_policy", "agent_prompt_version", "agent_model", "agent_max_steps",
  "agent_context_mode"]`. 659 in, 25 out. Parsed cleanly.
- It looked like a ranking (tools 1st, prompt 2nd), but it is **exactly the input order
  reversed**.

## 3. qwen3:14b, think off, shuffled order shown (seed "opspilot-eval-1-guided")

This uses the uncommitted change: `shown_order` (a seeded shuffle) plus echo detection.
- Shown order: `agent_context_mode, agent_prompt_version, agent_tool_policy, agent_model,
  agent_max_steps`
- Raw reply: `["agent_max_steps", "agent_model", "agent_tool_policy", "agent_prompt_version",
  "agent_context_mode"]`
- Parsed; the note fires: **"LLM returned the input order reversed"**. Positions: tool_policy
  3rd, prompt_version 4th.

Conclusion so far: with think off, neither local model ranks on this prompt. 8b echoes the
order and 14b reverses it.

## Dev-case diagnostics (opspilot-dev-1)

Dev case from `bench.make_incident --domain opspilot --seed 1 --set dev` (5 changes; the
release and incident are git-ignored, and the key wasn't opened), captured with
`sandbox.capture_bench --case opspilot-dev-1`. Every call uses qwen3:14b (the investigator
model) and the shuffled order seeded by "opspilot-dev-1-guided", with the same `_parse`. Run
in eval run 4's approval pause, with no replays running.

Note: the seeded shuffle for "opspilot-dev-1-guided" happens to equal the one for
"opspilot-eval-1-guided" (`context_mode, prompt_version, tool_policy, model, max_steps`), a
1-in-120 coincidence. Other seeds give other orders (checked for all six eval cases), so the
seeding works.

### 4. think off, 200 tokens
- Reply: `["agent_max_steps", "agent_model", "agent_tool_policy", "agent_prompt_version",
  "agent_context_mode"]`. 658 in, 25 out, 9.2 s.
- Note: **"LLM returned the input order reversed"**. prompt_version 4th, tool_policy 3rd.

### 5. think on (LLM_THINK=1), 2048 tokens
- Reply (JSON part): `["agent_prompt_version", "agent_model", "agent_tool_policy",
  "agent_context_mode", "agent_max_steps"]`. 652 in, about 1,230 out (mostly thinking),
  **59.2 s**.
- No echo note: it's neither the shown order nor its reverse. **prompt_version 1st**,
  tool_policy 3rd.

### 6. reasons prompt: not run
The brief ran it only if variant 5 echoed or failed, and 5 ranked.

**Result: the thinking variant (5) meets the bar** (agent_prompt_version or
agent_tool_policy in the top 2, and not an echo). Cost: about 60 s and about 1.2k output
tokens per guided investigation (one ranking call each), on top of about 11 min of search.
