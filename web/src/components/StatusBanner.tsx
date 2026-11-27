import type { InvestigationStatus } from "@/lib/types";
import { Icon, type IconName } from "./Icon";

type Tone = "green" | "amber" | "red" | "blue";

const TONE_ICON: Record<Tone, IconName> = {
  green: "checkCircle",
  amber: "alert",
  red: "x",
  blue: "info",
};

/** One plain sentence on what the current status means and what the human can do. */
function describe(status: InvestigationStatus, hasRollback: boolean): [Tone, string] | null {
  switch (status) {
    case "capturing":
    case "reproducing":
      return ["blue", "Reproducing the failure in the sandbox."];
    case "searching":
      return ["blue", "Searching for the smallest set of flag changes that causes the failure."];
    case "validating":
      return ["blue", "Testing repairs, narrowest first, on the incident and held-out cases."];
    case "proposed":
      return null; // the page itself is the ask
    case "approved":
      return ["blue", "Approved. The gate is writing the change to Flagsmith."];
    case "applied":
      return ["blue", "Written to Flagsmith. Reading back what Flagsmith serves (up to 60 s)."];
    case "verified":
      return ["green", "Verified: Flagsmith serves the repair to the affected identities."];
    case "failed":
      return hasRollback
        ? ["red", "The repair was written, but Flagsmith did not serve it as expected. A rollback is proposed below."]
        : ["red", "The investigation failed. Nothing further will be written without a new approval."];
    case "stale":
      return ["amber", "Flagsmith changed after the proposal, so the gate refused it. Nothing was written."];
    case "expired":
      return ["amber", "The approval window passed before anyone approved. Nothing was written."];
    case "rejected":
      return ["blue", "Rejected. Nothing was written to Flagsmith."];
    case "inconclusive":
      return ["amber", "The search could not settle within its run budget, so no change is proposed."];
    case "not_reproducible":
      return ["amber", "The failure did not reproduce in the sandbox, so no change is proposed."];
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
    <p className={`notice notice-${tone}`} role="status">
      <Icon name={TONE_ICON[tone]} />
      <span>{text}</span>
    </p>
  );
}
