# Demo video: script and shot list (2:30)

Record Saturday 3:30–5:30 AM ET, on Naman's Mac, with nothing else running on it (code freeze at
5:30 AM ET). Every number on screen must be
measured; anything replayed from cache is labelled "replayed from cache" on screen. Do one take of
the approve moment against live Flagsmith with no cache.

What the judges score in the video: does it clearly show the problem, what we built and how
Flagsmith fits in. Track 2 also scores: uses Flagsmith's CLI, API or MCP server; saves real time;
a human approves changes; clear output. Every shot below serves one of those.

## Before recording

- [ ] Naman's Mac: Ollama running with `qwen3:14b` and `qwen3:8b`; `.env` from `scripts/setup_env.sh`
- [ ] Production build: `docker compose up --build` with `MOCK=0` (no dev-server badge on screen); gate healthy
- [ ] The scene has loaded once before the take (the 3D stage takes a few seconds the first time)
- [ ] The Flagsmith dashboard on Naman's account (free plan: one seat), OpsPilot Prod open, flags visible
- [ ] About 3:15 AM ET: start one fresh OpsPilot investigation on bench-1 through the API (not
  `run_arms`, so no eval record). It reaches "Needs approval" in about 12 minutes; don't sign in
  yet. Reset bench-1 to fingerprint d50c5682 after the take
- [ ] VaultGuard interaction run ready (cached is fine, labelled)
- [ ] Claude Code open with the TrustGate MCP server configured (README, "Use TrustGate from Claude Code")
- [ ] `bench/results.md` generated from the eval records
- [ ] 1440x900 browser window, zoom 100%, notifications off, clean profile, no keys anywhere on screen

## Shots

| Time | Screen | Voice-over | Have ready |
| --- | --- | --- | --- |
| 0:00–0:12 | OpsPilot chat reports "payments-prod resolved"; the database view shows it still degraded. Flagsmith version history: 5 flags changed last night | "Our AI agent broke right after five feature flags changed at once. Which one? Rolling all five back throws away the improvements that work." | Hero trace, Flagsmith tab |
| 0:12–0:30 | Claude Code: "investigate the payments agent incident" calls TrustGate's MCP server. Cut to the inbox hangar: the OpsPilot robot on its pad, amber "Needs approval" | "You ask from Claude Code, through TrustGate's own MCP server. TrustGate holds no key that can write to Flagsmith: only a separate gate does, and it writes through Flagsmith's MCP server." | Claude Code window, inbox |
| 0:30–0:58 | Click the OpsPilot robot. The arena: sandbox pods glowing red and green, the robot holding the red culprit token, "agent_prompt_version" above it. Hover one pod to show "Search · 2/3 failed". Story panel: "agent_prompt_version broke OpsPilot" | "It replays the agent in sandboxes: every pod is one combination of the release's flags, graded on the database, not the agent's word. Delta debugging narrows five suspects to one. The obvious suspect, the cheaper model, is innocent." | Live or cached run |
| 0:58–1:18 | Approval panel: the red/green diff (`v2_fast` → `v1_strict` for tier0_production only), the checks in the story panel, "Expires in", then open Evidence for two seconds (repairs tested: segment override proposed, global rollback keeps 0%) | "It proves the narrowest repair: keep the faster prompt and the cheaper model everywhere, except for tier-0 customers. Here is the exact change it will make, and nothing happens until a human approves it." | Proposal waiting |
| 1:18–1:42 | Sign in, click Approve and apply. The gate's amber field dissolves, the doors slide open, the token turns green and flies to the Prod robot, which turns green. **Flagsmith moment 1:** cut to the dashboard, `agent_prompt_version`, the new `tier0_production` segment override. **Flagsmith moment 2:** its version history, the new feature version whose description carries the proposal and approval ids. Back to the arena: Verified | "One approval, one exact change, used once, written as a Flagsmith feature version through Flagsmith's MCP server. Then it checks what Flagsmith actually serves: tier-0 gets the strict prompt, everyone else keeps the fast one." | Live take, no cache; dashboard tab open on the feature |
| 1:42–1:50 | **Flagsmith moment 3:** open Evidence. The exact MCP call (`create_environment_feature_version` with its payload) and the gate's audit row for it: who approved, payload hash, fingerprints before and after | "Every write is one recorded MCP call, and the gate keeps its own hash-chained trail." | Same investigation |
| 1:50–2:08 | VaultGuard arena: the search finds the tools × prompt pair; the agent split a $14,500 invoice to slip under the approval line. The proposal: revert only `agent_tool_policy`, only for `high_value_wires` | "Same tool, a payments agent. This bug only appears when two flags change together. The narrowest fix wins: revert only the tools, only for high-value wires; the fast prompt stays." | Recorded VaultGuard eval run, labelled "recorded" |
| 2:08–2:30 | `bench/results.md` table | Lead with human active time versus the manual arm (seconds of clicks against minutes of work) and how much of the release each repair keeps; say n = 6 per arm and that the manual runner knew the culprit. End on the repo URL | Generated results |

Optional security beat (from `scripts/security_beats.py`): a reused approval refused, a stale
proposal refused, a denied read. Take its time from the VaultGuard shot.

## After recording

- [ ] Edit to 2:30 or less; captions on
- [ ] Upload unlisted; paste the link at the top of the README
- [ ] Tag `v1.0`
