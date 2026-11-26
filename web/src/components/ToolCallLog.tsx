import { formatClock } from "@/lib/format";
import type { AuditEntry } from "@/lib/types";

/** The gate's audit trail: every read, refused tool, approval and write. */
export function ToolCallLog({ entries }: { entries: AuditEntry[] }) {
  if (entries.length === 0) return <p className="muted">No gate activity yet.</p>;
  return (
    <ul className="log">
      {entries.map((e, i) => (
        <li key={i} className={`log-${e.action}`}>
          <span className="log-time">{formatClock(e.ts)}</span>
          <span className="log-actor">{e.actor}</span>
          <span className="log-action">{e.action}</span>
          {e.tool && <code>{e.tool}</code>}
          {e.result && <span className="muted">{e.result}</span>}
        </li>
      ))}
    </ul>
  );
}
