# TrustGate

**Your AI agent broke after five feature flags changed. Rolling everything back would throw away
the improvements that still work. TrustGate finds what broke, tests a targeted repair, and asks
for your approval before changing a single production flag.**

Built for [Toggle Wars](https://www.wemakedevs.org/hackathons/flagsmith), Track 2: AI Agents and
Feature Flags.

> Demo video: _coming Saturday_ · Status: in development (checkpoint CP0)

## How it works

_TODO (Arnab, Saturday): architecture diagram and a 5-line walkthrough._

1. A Flagsmith release changes several flags that control an AI agent (model, prompt, tools).
2. The agent misbehaves. TrustGate captures exactly what the failing agent received.
3. It replays the agent in a sandbox and uses delta debugging to find a minimal set of flag
   changes that reproduces the failure.
4. It tests repairs, narrowest first, and proposes the smallest one that passes: usually a
   segment override that protects only high-risk users.
5. A human approves the exact change. Only then does TrustGate write to Flagsmith, read the
   result back, and re-run the incident.

## How we use Flagsmith

_TODO (Naman, Saturday): required by the hackathon rules._ MCP server (allowlisted reads and the
approved write), CLI (environment snapshots and audit log), Python SDK (runtime flags and
read-back), segments (the repair itself).

## Quickstart

Requires Docker, or Python 3.12+ with [uv](https://docs.astral.sh/uv/) and Node 22+.

```bash
cp .env.example .env            # set APPROVER_PASSWORD at least; MOCK=1 serves demo data
cp .env.gate.example .env.gate  # the Flagsmith organisation key goes here, nowhere else
docker compose up --build
```

Open http://localhost:3000. Expected: an investigations list with an OpsPilot incident waiting
for approval. With `MOCK=1` every number on screen is fixture data.

Without Docker:

```bash
uv sync
uv run uvicorn api.main:app --reload --port 8000
cd web && npm install && npm run dev
```

On a OneDrive folder, set `UV_LINK_MODE=copy` before `uv sync`.

### Environment variables

See `.env.example` (API, agents, verifier) and `.env.gate.example` (gate only). _TODO: final
table with every variable, Saturday._

## Security model

_TODO (Naman)._ The investigator holds no Flagsmith key; only the gate does. Writes need a
single-use, expiring, signed approval bound to the exact payload.

## Search and testing

_TODO (Khyati)._ What "1-minimal" means, the PASS / FAIL / INCONCLUSIVE outcomes, and the
rule-of-three evidence line.

## Benchmark

_TODO (Arnab)._ Pre-registration (committed before the first eval run), the 4 arms, and
`bench/results.md` with n stated.

## Development

```bash
uv run pytest              # unit tests (no network, no LLM)
uv run pytest -m flagsmith # real Flagsmith, at checkpoints
uv run pytest -m llm       # real LLM calls, spends credits
uv run ruff check .
```

Team: Arnab (product, benchmark, integration), Naman (Flagsmith, gate, agent), Khyati
(sandboxes, search).
