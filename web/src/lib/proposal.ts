import { describeOp } from "./format";
import type { FlagValue, Investigation, RepairCandidate } from "./types";

/** Real candidates from the search carry no ops (only the chosen one is built), so describe
 * them from the culprit set and segment when ops are missing. */
export function describeCandidate(c: RepairCandidate, inv: Investigation): string {
  if (c.kind === "global") return `all ${inv.suspects.length} changed flags reverted`;
  if (c.ops.length === 1) return describeOp(c.ops[0]);
  if (c.ops.length > 1) return `${c.ops.length} flags changed`;
  const culprits = inv.culprit_set.map((f) => f.flag).join(", ") || "the culprit flags";
  switch (c.kind) {
    case "segment_override":
      return `${culprits} back to baseline for segment ${inv.segment ?? "?"}`;
    case "surgical":
      return `${culprits} back to baseline for everyone`;
  }
}

/** Flags a candidate touches, as a comparable key ("" when the search gave no ops). */
function opsKey(c: RepairCandidate): string {
  return c.ops
    .map((op) => `${op.feature}@${op.segment ?? ""}`)
    .sort()
    .join(",");
}

/** Which candidate is the proposed one. Per-flag surgical options share a kind, so match the
 * flags too (code rebuilds the chosen ops, so compare feature and segment, not the whole op). */
export function chosenCandidateIndex(candidates: RepairCandidate[], chosen: RepairCandidate): number {
  const key = opsKey(chosen);
  const exact = candidates.findIndex((c) => c.kind === chosen.kind && opsKey(c) === key);
  if (exact >= 0) return exact;
  const bare = candidates.findIndex((c) => c.kind === chosen.kind && c.ops.length === 0);
  return bare >= 0 ? bare : candidates.findIndex((c) => c.kind === chosen.kind);
}

/** The value a flag has in the release (what the repair replaces), when the diff knows it. */
export function releaseValue(inv: Investigation, flag: string): FlagValue | undefined {
  return inv.suspects.find((s) => s.flag === flag)?.new ?? undefined;
}
