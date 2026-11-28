# TrustGate benchmark results

Generated 2026-11-28 17:19 UTC from bench/records — do not edit by hand

n per arm: Manual 0 · Global rollback 6 · TrustGate (plain ddmin) 6 · TrustGate + LLM ranking 6

## Results

| Metric | Manual | Global rollback | TrustGate (plain ddmin) | TrustGate + LLM ranking |
| --- | --- | --- | --- | --- |
| Human active time (median) | — | — | 11s | 5s |
| Cost saving kept vs release (median) | — | 0% | 95% | 95% |
| Flags reverted unnecessarily (mean) | — | pending | pending | pending |
| Interaction cases found | — | pending | pending | pending |
| Final fixes failing fresh validation | — | — | 0 of 6 | 0 of 6 |
| Time to first suspect, correct runs (median) | — | pending | pending | pending |
| Median time to applied fix (range) | — | — | 12m 17s (11m 07s–14m 25s) | 14m 36s (11m 55s–21m 14s) |
| Correct attribution | — | pending | pending | pending |
| Diagnostic runs (median) | — | 0 | 7.5 | 12.5 |
| Inference cost (total) | — | $0.00 | $2.30 | $2.60 |

## Scaling: search runs by release size

| Release size | TrustGate (plain ddmin) | TrustGate + LLM ranking |
| --- | --- | --- |
| 5 | 7.5 (n=2) | 19.5 (n=2) |
| 10 | 8.5 (n=2) | 21.5 (n=2) |
| 20 | 9.5 (n=2) | 21.5 (n=2) |

Cell: median search runs over both domains (n = number of searches).

By domain (search runs; one search per cell):

| Release size | OpsPilot: TrustGate (plain ddmin) | OpsPilot: TrustGate + LLM ranking | VaultGuard: TrustGate (plain ddmin) | VaultGuard: TrustGate + LLM ranking |
| --- | --- | --- | --- | --- |
| 5 | 5 | 6 | 10 | 33 |
| 10 | 6 | 9 | 11 | 34 |
| 20 | 7 | 8 | 12 | 35 |

## What this shows

| Pre-registered expectation | Verdict | Numbers |
| --- | --- | --- |
| 1. TrustGate (LLM ranking, as registered) needs less human active time than the manual arm. | pending | needs both arms' active times |
| 2. TrustGate (LLM ranking, as registered) keeps more of the release's cost saving than global rollback, with no more held-out failures. | held | median 95% kept against 0%; 6 of 6 TrustGate repairs passed held-out validation; global rollback held-out 117/117 |
| 3. TrustGate (LLM ranking, as registered) finds two-flag interactions more often than the manual arm. | pending | needs the answer keys and the manual arm |
| 4. Guided search needs fewer search runs than plain at 10 and 20 changed flags. | failed | 10 flags: 21.5 against 8.5; 20 flags: 21.5 against 9.5 |

## Per case

| Case | Arm | Diagnostic runs | Time to first suspect | Cost kept | Right culprit |
| --- | --- | --- | --- | --- | --- |
| opspilot-eval-1 | Global rollback | 0 | — | 0% | — |
| opspilot-eval-1 | TrustGate (plain ddmin) | 5 | 2m 51s | 100% | — |
| opspilot-eval-1 | TrustGate + LLM ranking | 3 | 3m 47s | 100% | — |
| opspilot-eval-2 | Global rollback | 0 | — | 0% | — |
| opspilot-eval-2 | TrustGate (plain ddmin) | 5 | 3m 16s | 100% | — |
| opspilot-eval-2 | TrustGate + LLM ranking | 3 | 3m 33s | 100% | — |
| opspilot-eval-3 | Global rollback | 0 | — | 0% | — |
| opspilot-eval-3 | TrustGate (plain ddmin) | 5 | 2m 46s | 96% | — |
| opspilot-eval-3 | TrustGate + LLM ranking | 5 | 4m 05s | 96% | — |
| vaultguard-eval-1 | Global rollback | 0 | — | 0% | — |
| vaultguard-eval-1 | TrustGate (plain ddmin) | 10 | 4m 18s | 94% | — |
| vaultguard-eval-1 | TrustGate + LLM ranking | 20 | 7m 23s | 94% | — |
| vaultguard-eval-2 | Global rollback | 0 | — | 0% | — |
| vaultguard-eval-2 | TrustGate (plain ddmin) | 10 | 4m 48s | 94% | — |
| vaultguard-eval-2 | TrustGate + LLM ranking | 30 | 11m 19s | 94% | — |
| vaultguard-eval-3 | Global rollback | 0 | — | 0% | — |
| vaultguard-eval-3 | TrustGate (plain ddmin) | 10 | 4m 08s | 88% | — |
| vaultguard-eval-3 | TrustGate + LLM ranking | 20 | 7m 17s | 88% | — |

## What this doesn't show

- **Generality.** n = 6 per arm on two demo agents with synthetic incidents. Only one bad change per domain passed verification, so a domain's cases share their culprit and differ only in the safe changes around it.
- **A blind manual baseline.** The manual runners built the change pools and knew each domain's culprit, so manual times are a lower bound (README, pre-registration).
- **Behaviour after the write.** VERIFIED means Flagsmith serves the fix and the environment document holds it; the agent is not replayed after the write.
- **Production.** Every replay ran in the sandbox; only the repair writes went to a real Flagsmith test environment (bench-1).

## Notes

- Unsolved runs count toward n but are excluded from time medians (unsolved: Manual 0, Global rollback 0, TrustGate (plain ddmin) 0, TrustGate + LLM ranking 0).
- Human active time was not measured on 6 Global rollback runs, 1 TrustGate (plain ddmin) run, 1 TrustGate + LLM ranking run: the UI timer recorded 0 ms for an approval made from an idle or unfocused tab. Those runs are left out of that median, not counted as instant.
- Kept in the records but not in the TrustGate + LLM ranking totals: opspilot-eval-1 (inv_2bf932164f8a). Run before Deviation 3: with qwen3:8b the guided ranking returned the suspects in the order shown, so this run tested the same configurations as plain (59 runs each). Reported as a finding; the case was re-run under Deviation 3 (inv_8dcf65a250c9).
- Kept in the records but not in the TrustGate + LLM ranking totals: opspilot-eval-1 (inv_b6e7b8812cb5). The proposal expired because the approver was away for 15 minutes; nothing was written to Flagsmith. A procedure failure, not a TrustGate result; the case was re-run (inv_8dcf65a250c9).
- Attribution rows (unnecessary reverts, interaction cases, time to first correct suspect, correct attribution) are pending because the answer keys in bench/keys/ are still sealed.
- n = 6 per arm is a small demonstration, not grounds for broad statistical claims.
