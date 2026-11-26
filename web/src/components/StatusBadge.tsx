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

export function StatusBadge({ status }: { status: InvestigationStatus }) {
  return <span className={`badge badge-${TONE[status]}`}>{STATUS_LABEL[status]}</span>;
}

const OUTCOME_TONE: Record<TestOutcome, string> = {
  PASS: "good",
  FAIL: "bad",
  INCONCLUSIVE: "warn",
};

export function OutcomeBadge({ outcome }: { outcome: TestOutcome }) {
  return <span className={`badge badge-${OUTCOME_TONE[outcome]}`}>{outcome}</span>;
}
