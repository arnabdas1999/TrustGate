# Demo video: script and shot list (2:30)

Record on Saturday after the 16:00 IST code freeze. Every number on screen must be measured;
anything replayed from cache is labelled "replayed from cache" on screen. Do one take of the
approve moment against live Flagsmith with no cache.

## Before recording

- [ ] `docker compose up --build` with `MOCK=0`; gate healthy (`/health` from inside the network)
- [ ] OpsPilot hero incident captured and an investigation reached "proposed" (or cached run ready)
- [ ] VaultGuard interaction run ready (cached is fine, labelled)
- [ ] Flagsmith dashboard open on the OpsPilot Prod environment (project TrustGate), flags list visible
- [ ] Claude Code open with the TrustGate MCP server configured (see README)
- [ ] `bench/results.md` generated from the eval records
- [ ] Browser zoom 125%, notifications off, a clean browser profile, no keys anywhere on screen

## Shots

| Time | Screen | Voice-over | Have ready |
| --- | --- | --- | --- |
| 0:00–0:15 | OpsPilot chat reports "payments-prod resolved"; database view shows it still degraded. Flagsmith version history: 5 flags changed last night | "Our AI agent broke after five feature flags changed. Rolling everything back would throw away the improvements that still work." | Hero incident trace, Flagsmith tab |
| 0:15–0:45 | Claude Code: "investigate the payments agent incident" calls TrustGate's MCP server (fallback: UI button). TrustGate timeline and gate activity log: allowlisted reads, one refused read; incident matched to `tier0_production` | "TrustGate works on its own, but it holds no Flagsmith key. Every read goes through an allowlist." | Claude Code window, UI incident page |
| 0:45–1:10 | Search matrix filling in; 5 suspects narrow to `agent_prompt_version`; "N search runs vs 96 exhaustive" | "It replays exactly what the agent received, in a sandbox, and grades the database, not the agent's word." | Live or cached run |
| 1:10–1:35 | Proposal card: 3 options with violations, held-out passes and cost kept; evidence line "0 failures in 20 runs"; the exact change, marked "Needs your approval" | "Here's the smallest repair it could prove, and the exact change it wants to make." | Proposal visible |
| 1:35–1:55 | Sign in, click Approve. Flagsmith dashboard shows the new tier-0 override (new feature version). Read-back: tier-0 identity gets `v1_strict`, staging keeps `v2_fast`. Status turns Verified | "One approval, one exact change, used once. Then it checks what Flagsmith actually serves." | Live take, no cache |
| 1:55–2:15 | VaultGuard: search finds the tools × prompt pair; the agent had split a $25k wire. Repair: restricted tools on `high_value_wires` | "Same tool, different agent. This bug only appears when two flags change together." | Cached VaultGuard run, labelled |
| 2:15–2:30 | `bench/results.md` table | Lead with human active time and repair quality, then the other measured numbers, including n. End on the repo URL | Generated results |

Optional 10 s (only if the Experimentation stretch is done): Flagsmith's win probability, then
the approved rollout of the winner; take the time from the VaultGuard shot.

Optional security beat (from `scripts/security_beats.py`): a reused approval refused, a stale
proposal refused, a denied read.

## After recording

- [ ] Edit to 2:30 or less; captions on
- [ ] Upload unlisted; paste the link at the top of the README
- [ ] Tag `v1.0`
