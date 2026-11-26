import type { FlagValue, InvestigationStatus, RepairKind, WriteOp } from "./types";

export function durationMs(fromIso: string | null, toIso: string | null): number | null {
  if (!fromIso || !toIso) return null;
  return new Date(toIso).getTime() - new Date(fromIso).getTime();
}

export function formatDuration(ms: number | null): string {
  if (ms === null || Number.isNaN(ms)) return "—";
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes}m ${seconds.toString().padStart(2, "0")}s` : `${seconds}s`;
}

export function formatClock(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatValue(value: FlagValue | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function percent(fraction: number | null): string {
  return fraction === null ? "—" : `${Math.round(fraction * 100)}%`;
}

/** Rule of three: with 0 failures in n runs, the 95% upper bound on the failure rate is ~3/n. */
export function evidenceLine(runs: number, failures: number): string {
  if (failures > 0) return `${failures} failure${failures === 1 ? "" : "s"} in ${runs} runs.`;
  const bound = Math.min(100, Math.round((3 / runs) * 100));
  return `0 failures in ${runs} runs. With 95% confidence the failure rate is below about ${bound}%.`;
}

export const KIND_LABEL: Record<RepairKind, string> = {
  segment_override: "Segment override",
  surgical: "Surgical revert",
  global: "Global rollback",
};

export const STATUS_LABEL: Record<InvestigationStatus, string> = {
  capturing: "Capturing",
  reproducing: "Reproducing",
  searching: "Searching",
  validating: "Validating repair",
  proposed: "Needs approval",
  approved: "Approved",
  applied: "Applied, verifying",
  verified: "Verified",
  stale: "Stale: Flagsmith changed",
  expired: "Approval expired",
  failed: "Failed",
  rejected: "Rejected",
  inconclusive: "Inconclusive",
  not_reproducible: "Not reproducible",
};

export function describeOp(op: WriteOp): string {
  const where = op.segment ? `for segment ${op.segment}` : "for everyone";
  return `${op.feature} = ${formatValue(op.value)} ${where}`;
}
