# Demo video: script and shot list (2:30)

Record on Saturday after the 16:00 IST code freeze, on Naman's Mac. Every number on screen must be
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
- [ ] OpsPilot hero incident captured; its investigation reached "Needs approval", not signed in yet
- [ ] VaultGuard interaction run ready (cached is fine, labelled)
- [ ] Claude Code open with the TrustGate MCP server configured (README, "Use TrustGate from Claude Code")
- [ ] `bench/results.md` generated from the eval records
- [ ] 1440x900 browser window, zoom 100%, notifications off, clean profile, no keys anywhere on screen

## Shots

| Time | Screen | Voice-over | Have ready |
| --- | --- | --- | --- |
| 0:00–0:12 | OpsPilot chat reports "payments-prod resolved"; the database view shows it still degraded. Flagsmith version history: 5 flags changed last night | "Our AI agent broke right after five feature flags changed at once. Which one? Rolling all five back throws away the improvements that work." | Hero trace, Flagsmith tab |
| 0:12–0:30 | Claude Code: "investigate the payments agent incident" calls TrustGate's MCP server. Cut to the inbox hangar: the OpsPilot robot on its pad, amber "Needs approval" | "TrustGate is an agent that works with Flagsmith for you, through Flagsmith's MCP server. But it holds no key that can write: only a separate gate does." | Claude Code window, inbox |
| 0:30–0:58 | Click the OpsPilot robot. The arena: sandbox pods glowing red and green, the robot holding the red culprit token, "agent_prompt_version" above it. Hover one pod to show "Search · 2/3 failed". Story panel: "agent_prompt_version broke OpsPilot" | "It replays the agent in sandboxes: every pod is one combination of the release's flags, graded on the database, not the agent's word. Delta debugging narrows five suspects to one. The obvious suspect, the cheaper model, is innocent." | Live or cached run |
| 0:58–1:18 | Approval panel: the red/green diff (`v2_fast` → `v1_strict` for tier0_production only), the checks in the story panel, "Expires in", then open Evidence for two seconds (repairs tested: segment override proposed, global rollback keeps 0%) | "It proves the narrowest repair: keep the faster prompt and the cheaper model everywhere, except for tier-0 customers. Here is the exact change it will make, and nothing happens until a human approves it." | Proposal waiting |
| 1:18–1:42 | Sign in, click Approve and apply. The gate's amber field dissolves, the doors slide open, the token turns green and flies to the Prod robot, which turns green. Cut to the Flagsmith dashboard: the new tier-0 override (a new feature version). Back to the arena: Verified | "One approval, one exact change, used once, written through Flagsmith's MCP. Then it reads back what Flagsmith actually serves: tier-0 gets the strict prompt, everyone else keeps the fast one." | Live take, no cache |
| 1:42–2:05 | VaultGuard arena: the search finds the tools × prompt pair; the agent split a $14,500 invoice to slip under the approval line. Evidence: the segment-only fix fails held-out cases, so the proposal is the per-flag revert | "Same tool, a payments agent. This bug only appears when two flags change together, and TrustGate shows why the narrower fix was rejected." | Cached VaultGuard run, labelled |
| 2:05–2:30 | `bench/results.md` table | Lead with human active time versus the manual arm and how much of the release each repair keeps; say n. End on the repo URL | Generated results |

Optional security beat (from `scripts/security_beats.py`): a reused approval refused, a stale
proposal refused, a denied read. Take its time from the VaultGuard shot.

## After recording

- [ ] Edit to 2:30 or less; captions on
- [ ] Upload unlisted; paste the link at the top of the README
- [ ] Tag `v1.0`
