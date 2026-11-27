# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary audience this week: hackathon judges (Toggle Wars, Track 2: AI Agents and Feature
Flags) watching the 2:30 demo video and skimming the repo and UI. They must understand, in
seconds, what broke, what TrustGate found, and what it wants to change.

The product's working user, as the demo portrays it: an engineer reviewing an incident on an AI
agent (OpsPilot, an ops agent; VaultGuard, a payments agent) after a Flagsmith release, who
approves or rejects one exact Flagsmith change.

## Product Purpose

When an AI agent breaks after several feature flags change at once, TrustGate finds the exact
flag change that caused it, proves the narrowest repair, and asks a human to approve that one
change before anything is written to Flagsmith. Success: the right culprit, a repair that keeps
the release's other improvements, and a verified read-back, for about a minute of human time.

## Positioning

It replays the agent in a sandbox to find the exact flag change that broke it (delta debugging),
proves the narrowest repair (segment override, then per-flag revert, then global rollback), and
changes nothing in Flagsmith until a human approves it. The investigator holds no Flagsmith key;
only a separate gate does, and it writes only a signed, single-use, exact, approved change.

## Operating Context

- An investigation runs: reproduce, search (ddmin over the changed flags), confirm, validate
  repairs on the incident and held-out cases, propose.
- The approver signs in with a password and approves or rejects. The gate writes through
  Flagsmith's MCP server; the verifier reads back what Flagsmith serves to an identity inside the
  segment and one outside it, then the investigation is Verified, or a rollback is proposed.
- Every read and write goes through the gate's allowlist and hash-chained audit trail.
- The demo runs locally (Next.js UI, FastAPI API, gate service) with local models via Ollama.
  The video is recorded on this UI on Saturday after the 16:00 IST code freeze.

## Capabilities and Constraints

- Screens: investigations list; investigation page (timeline, budget, proposal card with repair
  options, evidence line, the exact Flagsmith call, approve/reject, read-back, stats; search
  matrix; gate activity log); rollback state.
- Stack: Next.js 16, React 19, plain CSS (no Tailwind), self-hosted fonts (no build-time
  downloads), runs offline and in Docker.
- MOCK=1 serves fixture data; every number in mock mode is fixture data.
- Terminology: investigation, culprit set, repair (segment override, surgical revert, global
  rollback), held-out cases, read-back, gate, approval, rollback proposal.

## Brand Commitments

- Name: TrustGate. Personality: trustworthy: careful, precise, accountable, a tool you'd let near
  production.
- Flagsmith is named factually (the platform it repairs); no Flagsmith branding or logo use.
- Visual standard (user's choice, Nov 27, after three rejected themed directions): a clean,
  conventional product UI with no themed metaphor, at the craft level of Linear, Stripe and
  Vercel.

## Evidence on Hand

- Real flows and data come from local runs; benchmark results (`bench/results.md`) do not exist
  yet. Do not fabricate customers, metrics, testimonials or results on screen.

## Product Principles

1. Nothing changes in production without an explicit, exact, human-approved change.
2. Show the evidence, then the ask: what broke, why, how sure, what will be written.
3. Prefer the narrowest repair that keeps the release's improvements.
4. Every number on screen is measured or clearly labelled as demo data.

## Accessibility & Inclusion

WCAG 2.2 AA: contrast, keyboard operation, visible focus, reduced motion, colour never the only
cue.
