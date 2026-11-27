"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { formatClock } from "@/lib/format";
import { usePolling } from "@/lib/hooks";
import type { Investigation, InvestigationStatus } from "@/lib/types";
import { Icon } from "./Icon";
import { StatusBadge } from "./StatusBadge";

const IN_PROGRESS = new Set<InvestigationStatus>([
  "capturing",
  "reproducing",
  "searching",
  "validating",
  "approved",
  "applied",
]);
const PROBLEM = new Set<InvestigationStatus>(["failed", "stale", "expired", "inconclusive", "not_reproducible"]);

function Kpis({ data }: { data: Investigation[] }) {
  const count = (pred: (s: InvestigationStatus) => boolean) => data.filter((i) => pred(i.status)).length;
  const items = [
    { label: "Needs approval", value: count((s) => s === "proposed"), tone: "warn", icon: "alert" as const },
    { label: "In progress", value: count((s) => IN_PROGRESS.has(s)), tone: "", icon: "search" as const },
    { label: "Verified", value: count((s) => s === "verified"), tone: "good", icon: "check" as const },
    { label: "Needs a look", value: count((s) => PROBLEM.has(s)), tone: "bad", icon: "x" as const },
  ];
  return (
    <div className="kpis">
      {items.map((k) => (
        <div key={k.label} className={`kpi${k.value > 0 && k.tone ? ` kpi-${k.tone}` : ""}`}>
          <div className="kpi-label">
            <Icon name={k.icon} size={14} />
            {k.label}
          </div>
          <div className="kpi-value">{k.value}</div>
        </div>
      ))}
    </div>
  );
}

export function InvestigationList() {
  const { data, error } = usePolling(api.investigations, 2000);
  const router = useRouter();

  if (!data) {
    return (
      <section className="card">
        {error ? (
          <p className="error">Could not reach the API: {error}</p>
        ) : (
          <div aria-busy="true" aria-label="Loading investigations" style={{ display: "grid", gap: 12 }}>
            <div className="skeleton" style={{ width: "60%" }} />
            <div className="skeleton" />
            <div className="skeleton" style={{ width: "80%" }} />
          </div>
        )}
      </section>
    );
  }

  return (
    <>
      <Kpis data={data} />
      <section className="card card-flush">
        {data.length === 0 ? (
          <div className="empty">
            <Icon name="shield" size={28} />
            <strong>No investigations yet</strong>
            <span className="small">They appear here when an agent alert starts one.</span>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Agent</th>
                  <th scope="col">Investigation</th>
                  <th scope="col">Status</th>
                  <th scope="col">Segment</th>
                  <th scope="col">Alert</th>
                  <th scope="col">Runs</th>
                </tr>
              </thead>
              <tbody>
                {data.map((inv) => {
                  const href = `/investigations/${inv.id}`;
                  const opspilot = inv.domain === "opspilot";
                  return (
                    <tr key={inv.id} className="row-link" onClick={() => router.push(href)}>
                      <td>
                        <span className="agent">
                          <span className="agent-mark">
                            <Icon name={opspilot ? "bot" : "vault"} size={14} />
                          </span>
                          {opspilot ? "OpsPilot" : "VaultGuard"}
                        </span>
                      </td>
                      <td>
                        <Link href={href} className="inv-id" onClick={(e) => e.stopPropagation()}>
                          {inv.id}
                        </Link>
                      </td>
                      <td>
                        <StatusBadge status={inv.status} />
                      </td>
                      <td>{inv.segment ? <code>{inv.segment}</code> : <span className="muted">—</span>}</td>
                      <td className="num">{formatClock(inv.timestamps.alert)}</td>
                      <td className="num">
                        {inv.runs_used} <span className="muted">/ {inv.budget_runs}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
