"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { formatClock } from "@/lib/format";
import { usePolling } from "@/lib/hooks";
import { StatusBadge } from "./StatusBadge";

export function InvestigationList() {
  const { data, error } = usePolling(api.investigations, 2000);

  if (!data) return <p className="muted">{error ? `Could not reach the API: ${error}` : "Loading…"}</p>;
  if (data.length === 0) return <p className="muted">No investigations yet.</p>;

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Agent</th>
            <th>Investigation</th>
            <th>Status</th>
            <th>Segment</th>
            <th>Alert</th>
            <th>Runs</th>
          </tr>
        </thead>
        <tbody>
          {data.map((inv) => (
            <tr key={inv.id}>
              <td>{inv.domain === "opspilot" ? "OpsPilot" : "VaultGuard"}</td>
              <td>
                <Link href={`/investigations/${inv.id}`}>{inv.id}</Link>
              </td>
              <td>
                <StatusBadge status={inv.status} />
              </td>
              <td>{inv.segment ? <code>{inv.segment}</code> : "—"}</td>
              <td>{formatClock(inv.timestamps.alert)}</td>
              <td>
                {inv.runs_used} / {inv.budget_runs}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
