# TrustGate benchmark results

Generated 2026-11-28 04:57 UTC from bench/records — do not edit by hand

n per arm: Manual 0 · Global rollback 0 · Plain ddmin 6 · TrustGate 6

## Results

| Metric | Manual | Global rollback | Plain ddmin | TrustGate |
| --- | --- | --- | --- | --- |
| Human active time (median) | — | — | 11s | 5s |
| Cost saving kept vs release (median) | — | — | 95% | 95% |
| Flags reverted unnecessarily (mean) | — | — | pending | pending |
| Interaction cases found | — | — | pending | pending |
| Final fixes failing fresh validation | — | — | 0 of 6 | 0 of 6 |
| Time to first suspect, correct runs (median) | — | — | pending | pending |
| Median time to applied fix (range) | — | — | 12m 17s (11m 07s–14m 25s) | 14m 36s (11m 55s–21m 14s) |
| Correct attribution | — | — | pending | pending |
| Diagnostic runs (median) | — | — | 7.5 | 12.5 |
| Inference cost (total) | — | — | $2.30 | $2.60 |

## Scaling: search runs by release size

| Release size | Plain ddmin | TrustGate |
| --- | --- | --- |
| 5 | — | — |
| 10 | — | — |
| 20 | — | — |

Cell: median search runs (n = number of searches).

## What this shows

| Pre-registered expectation | Verdict | Numbers |
| --- | --- | --- |
| 1. TrustGate needs less human active time than the manual arm. | pending | needs both arms' active times |
| 2. TrustGate keeps more of the release's cost saving than global rollback, with no more held-out failures. | pending | needs the global rollback arm |
| 3. TrustGate finds two-flag interactions more often than the manual arm. | pending | needs the answer keys and the manual arm |
| 4. Guided search needs fewer search runs than plain at 10 and 20 changed flags. | pending | needs the scaling test |

## Per case

| Case | Arm | Diagnostic runs | Time to first suspect | Cost kept | Right culprit |
| --- | --- | --- | --- | --- | --- |
| opspilot-eval-1 | Plain ddmin | 5 | 2m 51s | 100% | — |
| opspilot-eval-1 | TrustGate | 3 | 3m 47s | 100% | — |
| opspilot-eval-2 | Plain ddmin | 5 | 3m 16s | 100% | — |
| opspilot-eval-2 | TrustGate | 3 | 3m 33s | 100% | — |
| opspilot-eval-3 | Plain ddmin | 5 | 2m 46s | 96% | — |
| opspilot-eval-3 | TrustGate | 5 | 4m 05s | 96% | — |
| vaultguard-eval-1 | Plain ddmin | 10 | 4m 18s | 94% | — |
| vaultguard-eval-1 | TrustGate | 20 | 7m 23s | 94% | — |
| vaultguard-eval-2 | Plain ddmin | 10 | 4m 48s | 94% | — |
| vaultguard-eval-2 | TrustGate | 30 | 11m 19s | 94% | — |
| vaultguard-eval-3 | Plain ddmin | 10 | 4m 08s | 88% | — |
| vaultguard-eval-3 | TrustGate | 20 | 7m 17s | 88% | — |

## What this doesn't show

- **Generality.** n = 6 per arm on two demo agents with synthetic incidents. Only one bad change per domain passed verification, so a domain's cases share their culprit and differ only in the safe changes around it.
- **A blind manual baseline.** The manual runners built the change pools and knew each domain's culprit, so manual times are a lower bound (README, pre-registration).
- **Behaviour after the write.** VERIFIED means Flagsmith serves the fix and the environment document holds it; the agent is not replayed after the write.
- **Production.** Every replay ran in the sandbox; only the repair writes went to a real Flagsmith test environment (bench-1).

## Notes

- Unsolved runs count toward n but are excluded from time medians (unsolved: Manual 0, Global rollback 0, Plain ddmin 0, TrustGate 0).
- Human active time was not measured on 1 Plain ddmin run, 1 TrustGate run: the UI timer recorded 0 ms for an approval made from an idle or unfocused tab. Those runs are left out of that median, not counted as instant.
- Kept in the records but not in the TrustGate totals: opspilot-eval-1 (inv_2bf932164f8a). Run before Deviation 3: with qwen3:8b the guided ranking returned the suspects in the order shown, so this run tested the same configurations as plain (59 runs each). Reported as a finding; the case was re-run under Deviation 3 (inv_8dcf65a250c9).
- Kept in the records but not in the TrustGate totals: opspilot-eval-1 (inv_b6e7b8812cb5). The proposal expired because the approver was away for 15 minutes; nothing was written to Flagsmith. A procedure failure, not a TrustGate result; the case was re-run (inv_8dcf65a250c9).
- Attribution rows (unnecessary reverts, interaction cases, time to first correct suspect, correct attribution) are pending because the answer keys in bench/keys/ are still sealed.
- n = 6 per arm is a small demonstration, not grounds for broad statistical claims.
