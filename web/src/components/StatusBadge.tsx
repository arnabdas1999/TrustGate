import { STATUS_LABEL } from "@/lib/format";
import type { InvestigationStatus } from "@/lib/types";

export const STATUS_TONE: Record<InvestigationStatus, "amber" | "blue" | "green" | "red" | "grey"> = {
  capturing: "blue",
  reproducing: "blue",
  searching: "blue",
  validating: "blue",
  proposed: "amber",
  approved: "blue",
  applied: "blue",
  verified: "green",
  stale: "red",
  expired: "grey",
  failed: "red",
  rejected: "grey",
  inconclusive: "amber",
  not_reproducible: "grey",
};

/** Work in progress: the dot breathes (static under reduced motion). */
export const LIVE = new Set<InvestigationStatus>([
  "capturing",
  "reproducing",
  "searching",
  "validating",
  "approved",
  "applied",
]);

export function StatusBadge({ status }: { status: InvestigationStatus }) {
  const classes = ["pill", `pill-${STATUS_TONE[status]}`];
  if (LIVE.has(status)) classes.push("pill-live");
  return (
    <span className={classes.join(" ")}>
      <span className="pill-dot" aria-hidden />
      {STATUS_LABEL[status]}
    </span>
  );
}
