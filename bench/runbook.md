# Manual arm runbook (1 page)

You are the on-call engineer. An AI agent misbehaved after last night's Flagsmith release.
Find the change that caused it and fix it in Flagsmith. Work the way you normally would.

## Before you start (timer, not you)

- You have **not** seen this case before, and you have not opened anything in `bench/keys/`.
- Your environment is yours alone (`bench-1`, `bench-2` or `bench-3`). Nobody else edits it.
- The timer starts the clock when you open the alert, and records your **first suspect** the
  moment you say it out loud.

## What you get

- The alert: the failing agent's trace and the grader's verdict.
- The release: `bench/releases/<case>.json` (which flags changed, old and new values).
- The Flagsmith dashboard for your environment.
- The replay command (Khyati's sandbox) to run the agent with any flag values you choose:

  ```
  uv run python -m sandbox.replay --domain <domain> --case <case> --set flag=value ...
  ```

  It prints the grader's PASS or FAIL. You may run it as often as you like.

## What counts as done

1. You change Flagsmith (any change you think is right: rollback, revert one flag, segment
   override, anything).
2. You replay the incident against the new configuration and it passes.
3. You say "done". The timer stops the clock.

## Rules

- No TrustGate, no LLM assistants, no asking teammates.
- Say your current suspect out loud whenever it changes; the timer notes each one.
- If you are stuck after 30 minutes, say "give up". That run is recorded as unsolved.

## The timer records

| Field | Example |
| --- | --- |
| Case | opspilot-eval-3 |
| Person | |
| Start / first suspect / done | 14:02:10 / 14:06:45 / 14:21:30 |
| Replays run | 7 |
| Final Flagsmith change | prompt reverted globally |
| Correct culprit? Minimal fix? | yes / no |
| Notes | |
