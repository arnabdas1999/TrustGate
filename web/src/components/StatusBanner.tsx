import type { InvestigationStatus } from "@/lib/types";
import { Icon, type IconName } from "./Icon";

type Tone = "good" | "warn" | "bad" | "neutral";

const TONE_ICON: Record<Tone, IconName> = {
  good: "check",
  warn: "alert",
  bad: "x",
  neutral: "info",
};

/** One plain sentence on what the current status means and what the human can do. */
function describe(status: InvestigationStatus, hasRollback: boolean): [Tone, string] | null {
  switch (status) {
    case "capturing":
    case "reproducing":
      return ["neutral", "Reproducing the failure in the sandbox."];
    case "searching":
      return ["neutral", "Searching for the smallest set of flag changes that causes the failure."];
    case "validating":
      return ["neutral", "Testing repairs, narrowest first, on the incident and held-out cases."];
    case "proposed":
      return null; // the proposal card says it
    case "approved":
      return ["neutral", "Approved. The gate is writing the change to Flagsmith."];
    case "applied":
      return ["neutral", "Written to Flagsmith. Reading back what Flagsmith serves (up to 60 s)."];
    case "verified":
      return ["good", "Verified: Flagsmith serves the repair to the affected identities."];
    case "failed":
      return hasRollback
        ? ["bad", "The repair was written, but Flagsmith did not serve it as expected. A rollback is proposed below."]
        : ["bad", "The investigation failed. Nothing further will be written without a new approval."];
    case "stale":
      return ["warn", "Flagsmith changed after the proposal, so the gate refused it. Nothing was written."];
    case "expired":
      return ["warn", "The approval window passed before anyone approved. Nothing was written."];
    case "rejected":
      return ["neutral", "Rejected. Nothing was written to Flagsmith."];
    case "inconclusive":
      return ["warn", "The search could not settle within its run budget, so no change is proposed."];
    case "not_reproducible":
      return ["warn", "The failure did not reproduce in the sandbox, so no change is proposed."];
  }
}

export function StatusBanner({
  status,
  hasRollback,
}: {
  status: InvestigationStatus;
  hasRollback: boolean;
}) {
  const info = describe(status, hasRollback);
  if (!info) return null;
  const [tone, text] = info;
  return (
    <p className={`banner banner-${tone}`} role="status">
      <Icon name={TONE_ICON[tone]} />
      <span>{text}</span>
    </p>
  );
}
