import { durationMs, formatDuration } from "@/lib/format";
import type { Timestamps } from "@/lib/types";

const STEPS: { key: keyof Timestamps; label: string }[] = [
  { key: "alert", label: "Alert" },
  { key: "first_suspect", label: "Culprit found" },
  { key: "proposal", label: "Proposed" },
  { key: "approved", label: "Approved" },
  { key: "verified", label: "Verified" },
];

/** One quiet line of progress: done steps in ink with the time since the alert. */
export function Timeline({ timestamps }: { timestamps: Timestamps }) {
  return (
    <ol className="progress" aria-label="Progress">
      {STEPS.map(({ key, label }) => {
        const at = timestamps[key];
        return (
          <li key={key} className={at ? "done" : undefined}>
            <span className="node" aria-hidden />
            {label}
            {key !== "alert" && at && (
              <span className="when">+{formatDuration(durationMs(timestamps.alert, at))}</span>
            )}
            {!at && <span className="sr-only">(not yet)</span>}
          </li>
        );
      })}
    </ol>
  );
}
