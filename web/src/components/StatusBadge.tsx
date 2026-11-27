import { STATUS_LABEL } from "@/lib/format";
import type { InvestigationStatus, TestOutcome } from "@/lib/types";

const TONE: Record<InvestigationStatus, string> = {
  capturing: "neutral",
  reproducing: "neutral",
  searching: "neutral",
  validating: "neutral",
  proposed: "warn",
  approved: "neutral",
  applied: "neutral",
  verified: "good",
  stale: "bad",
  expired: "bad",
  failed: "bad",
  rejected: "bad",
  inconclusive: "warn",
  not_reproducible: "warn",
};

/** Work in progress: the dot pulses (static under reduced motion). */
const LIVE = new Set<InvestigationStatus>([
  "capturing",
  "reproducing",
  "searching",
  "validating",
  "approved",
  "applied",
]);

export function StatusBadge({ status, large }: { status: InvestigationStatus; large?: boolean }) {
  const classes = ["badge", `badge-${TONE[status]}`];
  if (LIVE.has(status)) classes.push("badge-live");
  if (large) classes.push("badge-lg");
  return (
    <span className={classes.join(" ")}>
      <span className="badge-dot" aria-hidden />
      {STATUS_LABEL[status]}
    </span>
  );
}

const OUTCOME_TONE: Record<TestOutcome, string> = {
  PASS: "good",
  FAIL: "bad",
  INCONCLUSIVE: "warn",
};

const OUTCOME_LABEL: Record<TestOutcome, string> = {
  PASS: "Pass",
  FAIL: "Fail",
  INCONCLUSIVE: "Inconclusive",
};

export function OutcomeBadge({ outcome }: { outcome: TestOutcome }) {
  return (
    <span className={`badge badge-${OUTCOME_TONE[outcome]}`}>
      <span className="badge-dot" aria-hidden />
      {OUTCOME_LABEL[outcome]}
    </span>
  );
}
