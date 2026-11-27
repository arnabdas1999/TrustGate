# Decisions

One line per decision that changes how code is written. Newest first.

| Date | Decision | By | Why |
| --- | --- | --- | --- |
| 2026-11-27 | LLM is Ollama, run locally on Naman's Mac: `model_flagship` = `qwen3:14b`, `model_cheap` = `qwen3:8b` (replaced `llama3.2:3b`, which stopped after one tool call; with the procedure added to `v1_strict` in PR #6, qwen3:8b passes under `v1_strict` and fails under `v2_fast`), native `/api/chat` with `think=false`, `SANDBOX_PARALLEL=1`. Groq's free tier is the backup (`LLM_PROVIDER=groq`); Gemini stays supported. Prices keep notional defaults so cost comparisons stay meaningful | Naman, Arnab | Free Gemini allows ~20 requests/day; one investigation needs 600-1,400 calls. Spike: 0 malformed tool calls in 40 runs |
| 2026-11-27 | One Flagsmith project `TrustGate` (56664) with an environment per agent: `OpsPilot Prod` (121198), `VaultGuard Prod` (121309). Config: `TRUSTGATE_ENVIRONMENT_OPSPILOT` / `_VAULTGUARD`, keys `FLAGSMITH_SERVER_KEY_OPSPILOT_PROD` / `_VAULTGUARD_PROD` | Naman | Free plan allows one project |
| 2026-11-27 | `budget_runs` default raised from 160 to 240 runs | Naman, Arnab | Two-domain search plus repair validation needed the headroom |
| 2026-11-26 | (Replaced 11-27 by Ollama) LLM provider is the Gemini API (AI Studio key on Arnab's account). `model_cheap` maps to a Flash-class model, `model_flagship` to a Pro-class model; exact IDs set in `.env` | Arnab | Team already has Gemini access; one provider for agents and investigator |
| 2026-11-26 | `agent_temperature` is a string flag (`"0.0"`); read it with `FlagConfig.get_float` | Naman | Flagsmith remote config value; parsing in one place |
| 2026-11-26 | `agent_safe_fallback` is an enabled flag with boolean value `false` | Naman | Kill switch read as a boolean |
| 2026-11-26 | `FlagConfig.segment_overrides` added so replays can validate segment-scoped repairs | Arnab | Khyati's segment-aware replay (Friday) needs it |
| 2026-11-26 | Write path is MCP: v2 feature versioning is on in every environment and can't be turned off, so the gate writes with `create_environment_feature_version` + `publish_immediately`, one call per feature (not atomic across flags). The three WriteOp tools are logical operations | Naman | Hour-one spike: v2 write and cleanup verified on Development |
