# TrustGate

Finds which Flagsmith flag change broke an AI agent (OpsPilot, VaultGuard) by
replaying it in a sandbox (delta debugging), then proposes a segment-scoped
repair that a human approves. Hackathon: Toggle Wars Track 2, submit by
Sun Nov 29 9:30 AM IST.

Plan doc (source of truth for scope and tasks):
https://claude.ai/artifact/Y2dedK54p88CAEUoPyjdHi (tabs: plan, Development plan)

## Rules
- Only gate/ talks to Flagsmith with the organisation key. Nothing else
  imports gate internals; use /gate/read and /gate/write.
- Agents and the verifier use the server-side environment key only.
- The LLM never builds a write payload. Code builds WriteOps from a
  validated RepairCandidate.
- Replays use captured config plus overrides. Never toggle live flags
  during search. Never swap the model under test.
- Traits (env, tier, amount_usd, new_vendor) are set in code only.
- contracts/ changes need a PR approved by Arnab and the affected owner.
- Never read or print .env files. Never commit keys.

## Owners
- gate/, agent/, orchestrator/, verifier/, mcp_server/, scripts/release.py: Naman
- sandbox/, search/, regressions/: Khyati
- contracts/, api/ skeleton, web/, bench/, scripts/smoke.py, CI, compose: Arnab
- gate/service.py is a stub from CP0 so compose starts; Naman replaces it.

## Layout
- Python packages live at the repo root (contracts, api, gate, ...). Nothing is
  installed as a package; run everything from the repo root.
- web/ is the Next.js UI. It talks to the API at NEXT_PUBLIC_API_URL.
- MOCK=1 makes the API serve fixture data (api/mock_data.py).

## Commands
- Install: uv sync   (inside OneDrive set UV_LINK_MODE=copy first: hardlinks fail there)
- Unit tests: uv run pytest            (skips flagsmith and llm markers)
- Flagsmith tests: uv run pytest -m flagsmith
- LLM tests (costs credits): uv run pytest -m llm
- Lint: uv run ruff check .
- API locally: uv run uvicorn api.main:app --env-file .env --reload --port 8000
- Gate locally (real keys only): uv run uvicorn gate.service:app --env-file .env.gate --port 8100
- Web locally: cd web, then npm run dev
- Smoke test: uv run python -m scripts.smoke (APPROVER_PASSWORD in the shell)
- Everything: docker compose up --build   (WEB_PORT=3001 WEB_ORIGIN=http://localhost:3001 if 3000 is taken)
- New benchmark incident: uv run python -m bench.make_incident --domain opspilot --seed 1 --set dev --allow-unverified

## Git
- Each person works on their own branch: arnab-dev, naman-dev, khyati-dev.
  Start from arnab-dev (CP0 skeleton and contracts). Everything merges into
  main at the end; Arnab merges. Never commit straight to main.
- main is GitHub's default branch and stays empty until the final merge.
  Merge into main through PRs (arnab-dev -> main, naman-dev -> main, ...).
- Commit messages: "area: what changed".
- CI runs on pushes to *-dev branches and on PRs. Checkpoints are tagged cpN-name.
