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

Two files, never committed. `.env` is read by the API, the demo agents and the verifier.
`.env.gate` is read **only** by the gate container, because it holds the Flagsmith organisation
key.

| Variable | File | Needed for | What it is |
| --- | --- | --- | --- |
| `APPROVER_PASSWORD` | `.env` | Approving in the UI | Approver login. Login is disabled while empty |
| `APPROVER_NAME` | `.env` | Audit trail | Name recorded on approvals |
| `APPROVAL_SIGNING_SECRET` | both | Real writes | Shared HMAC secret; the gate accepts only approvals signed with it |
| `FLAGSMITH_SERVER_KEY_OPSPILOT_PROD`, `..._VAULTGUARD_PROD` | `.env` | Agents, verifier | Server-side environment keys (`ser.…`) for local evaluation |
| `FLAGSMITH_SERVER_KEY_BENCH_1` … `_3` | `.env` | Benchmark | One server-side key per benchmark environment |
| `LLM_PROVIDER` | `.env` | Agents, investigator | `gemini` (Gemini API key from Google AI Studio) |
| `LLM_API_KEY` | `.env` | Agents, investigator | The Gemini API key. A Gemini subscription does not include API usage |
| `MODEL_CHEAP`, `MODEL_FLAGSHIP` | `.env` | Agents | Real model IDs behind the flag values `model_cheap` and `model_flagship` |
| `GATE_URL` | `.env` | API | Where the API reaches the gate (`http://gate:8100` in compose) |
| `WEB_ORIGIN` | `.env` | API | Allowed browser origin (CORS), default `http://localhost:3000` |
| `MOCK` | `.env` | Development | `1` serves fixture data so the UI works without the backend |
| `FLAGSMITH_API_KEY` | `.env.gate` | Gate | Flagsmith organisation API key. Lives nowhere else |
| `FLAGSMITH_MCP_URL`, `FLAGSMITH_API_URL` | `.env.gate` | Gate | Flagsmith endpoints (defaults to Flagsmith Cloud) |
| `FLAGSMITH_PROJECT_ID` | `.env.gate` | Gate | The only project writes may touch |

The web app reads `NEXT_PUBLIC_API_URL` at build time (default `http://localhost:8000`).

## Security model

_TODO (Naman)._ The investigator holds no Flagsmith key; only the gate does. Writes need a
single-use, expiring, signed approval bound to the exact payload.

## Search and testing

_TODO (Khyati)._ What "1-minimal" means, the PASS / FAIL / INCONCLUSIVE outcomes, and the
rule-of-three evidence line.

## Benchmark

Results: `bench/results.md`, generated from records by `uv run python -m bench.results`. Every
number there was measured, bad runs included.

### Pre-registration

> **Status: DRAFT.** This section is frozen by the commit tagged `cp7-prereg`, made before the
> first eval run. Nothing below changes after that commit; any later change is listed with its
> reason under "Deviations".

**Questions.** Does TrustGate cut the human time needed to fix a flag-caused agent regression,
and does it keep more of the release than a blind rollback? Does LLM-guided search beat plain
delta debugging?

**Arms.**

| Arm | What it is |
| --- | --- |
| Manual | A teammate investigates with the Flagsmith dashboard, a replay command and `bench/runbook.md` |
| Global rollback | Every changed flag reverted (computed in the sandbox, no human) |
| Plain ddmin | TrustGate's search with LLM suspect ordering switched off |
| TrustGate | LLM-guided search; a human approves the proposed repair |

**Cases.** 6 eval cases, 3 OpsPilot and 3 VaultGuard, generated with
`bench/make_incident.py --set eval` from verified change pools. Each release has 4 or 5 flag
changes with one bad change (or bad pair) hidden among safe ones. Answer keys are salted and
sealed; only their SHA-256 hashes are committed before the runs (`bench/keys/hashes.txt`).
Target mix: 1 to 2 of the 6 cases are two-flag interactions. _Final seeds and the exact mix are
filled in here at CP7._ Dev cases used for tuning are never reported.

**Procedure.**

- Every arm runs the same 6 cases. Nobody approves an automated-arm case they investigated by
  hand. Each runner uses their own Flagsmith environment (`bench-1` to `bench-3`).
- Clock: from the alert to a repair applied in Flagsmith with the incident replay passing.
- Stop rules: the manual arm gives up after 30 minutes; automated arms stop at their run or
  dollar budget. Both count as unsolved, never as excluded.

**Metrics** (lead metrics first): human active time; cost saving kept versus the release; flags
reverted unnecessarily; interaction cases found; final fixes failing a fresh validation;
time to first correct suspect; time to applied fix; correct attribution; diagnostic runs;
inference cost. Scaling test: search runs at 5, 10 and 20 changed flags, plain versus guided.

**Expectations, stated before running.**

1. TrustGate needs less human active time than the manual arm.
2. TrustGate keeps more of the release's cost saving than global rollback, with no more
   held-out failures.
3. TrustGate finds two-flag interactions more often than the manual arm.
4. Guided search needs fewer search runs than plain ddmin at 10 and 20 changed flags; at 5 they
   may tie.

**Analysis.** Medians and ranges per arm with n stated; unsolved runs count toward n and are
excluded from time medians. Six cases per arm is a small demonstration, not grounds for broad
statistical claims. Expectations that fail are reported as failed.

**Deviations.** _None yet._

## Development

```bash
uv run pytest              # unit tests (no network, no LLM)
uv run pytest -m flagsmith # real Flagsmith, at checkpoints
uv run pytest -m llm       # real LLM calls, spends credits
uv run ruff check .
```

Team: Arnab (product, benchmark, integration), Naman (Flagsmith, gate, agent), Khyati
(sandboxes, search).
