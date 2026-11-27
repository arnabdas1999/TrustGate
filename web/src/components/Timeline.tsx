import { durationMs, formatClock, formatDuration } from "@/lib/format";
import type { Timestamps } from "@/lib/types";

const STEPS: { key: keyof Timestamps; label: string }[] = [
  { key: "alert", label: "Alert" },
  { key: "first_suspect", label: "First suspect" },
  { key: "proposal", label: "Proposal ready" },
  { key: "approved", label: "Approved" },
  { key: "verified", label: "Verified" },
];

/** A stepper on a rail: done steps are green, the latest one is ringed, pending ones hollow. */
export function Timeline({ timestamps }: { timestamps: Timestamps }) {
  const lastDone = STEPS.reduce((last, step, i) => (timestamps[step.key] ? i : last), -1);
  return (
    <ol className="timeline" aria-label="Investigation timeline">
      {STEPS.map(({ key, label }, i) => {
        const at = timestamps[key];
        const classes = [at ? "done" : "pending"];
        if (i === lastDone) classes.push("current");
        return (
          <li key={key} className={classes.join(" ")} aria-current={i === lastDone ? "step" : undefined}>
            <span className="timeline-label">{label}</span>
            <span className="timeline-time">{formatClock(at)}</span>
            {key !== "alert" && at && (
              <span className="timeline-delta">+{formatDuration(durationMs(timestamps.alert, at))}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
