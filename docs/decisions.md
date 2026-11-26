# Decisions

One line per decision that changes how code is written. Newest first.

| Date | Decision | By | Why |
| --- | --- | --- | --- |
| 2026-11-26 | LLM provider is the Gemini API (AI Studio key on Arnab's account). `model_cheap` maps to a Flash-class model, `model_flagship` to a Pro-class model; exact IDs set in `.env` | Arnab | Team already has Gemini access; one provider for agents and investigator |
| 2026-11-26 | `agent_temperature` is a string flag (`"0.0"`); read it with `FlagConfig.get_float` | Naman | Flagsmith remote config value; parsing in one place |
| 2026-11-26 | `agent_safe_fallback` is an enabled flag with boolean value `false` | Naman | Kill switch read as a boolean |
| 2026-11-26 | `FlagConfig.segment_overrides` added so replays can validate segment-scoped repairs | Arnab | Khyati's segment-aware replay (Friday) needs it |
| 2026-11-26 | Write path (MCP or Management API): _pending CP1_ | Naman | Decided by the hour-one spike |
