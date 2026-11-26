import { durationMs, formatClock, formatDuration } from "@/lib/format";
import type { Timestamps } from "@/lib/types";

const STEPS: { key: keyof Timestamps; label: string }[] = [
  { key: "alert", label: "Alert" },
  { key: "first_suspect", label: "First suspect" },
  { key: "proposal", label: "Proposal ready" },
  { key: "approved", label: "Approved" },
  { key: "verified", label: "Verified" },
];

export function Timeline({ timestamps }: { timestamps: Timestamps }) {
  return (
    <ol className="timeline">
      {STEPS.map(({ key, label }) => {
        const at = timestamps[key];
        return (
          <li key={key} className={at ? "done" : "pending"}>
            <span className="timeline-label">{label}</span>
            <span className="timeline-time">{formatClock(at)}</span>
            {key !== "alert" && at && (
              <span className="muted">+{formatDuration(durationMs(timestamps.alert, at))}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
