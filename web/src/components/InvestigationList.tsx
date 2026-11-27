"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { STATUS_LABEL, formatClock } from "@/lib/format";
import { useDemoMode, usePolling, useReducedMotion } from "@/lib/hooks";
import type { Investigation } from "@/lib/types";
import type { HangarItem } from "./arena/Hangar";
import type { Mood } from "./arena/Robot";
import { Icon } from "./Icon";
import { STATUS_TONE, StatusBadge } from "./StatusBadge";

const Hangar = dynamic(() => import("./arena/Hangar"), {
  ssr: false,
  loading: () => <div className="arena arena-loading" aria-hidden />,
});

const TEAM = { opspilot: "#f59e0b", vaultguard: "#14b8a6" } as const;
const AGENT = { opspilot: "OpsPilot", vaultguard: "VaultGuard" } as const;

function mood(inv: Investigation): Mood {
  if (["capturing", "reproducing", "searching", "validating"].includes(inv.status)) return "search";
  if (inv.status === "proposed") return "alert";
  if (inv.status === "verified") return "happy";
  if (["failed", "stale", "inconclusive", "not_reproducible"].includes(inv.status)) return "sad";
  return "idle";
}

function summary(inv: Investigation): string {
  const culprit = inv.culprit_set.map((c) => c.flag).join(" + ");
  if (culprit) return `${culprit} · segment ${inv.segment ?? "—"}`;
  return `${inv.runs_used} of ${inv.budget_runs} runs · ${inv.suspects.length} flags changed`;
}

export function InvestigationList() {
  const { data, error } = usePolling(api.investigations, 2000);
  const router = useRouter();
  const reduced = useReducedMotion();
  const demo = useDemoMode();

  const items: HangarItem[] = (data ?? []).slice(0, 4).map((inv) => ({
    id: inv.id,
    team: TEAM[inv.domain],
    agent: AGENT[inv.domain],
    status: STATUS_LABEL[inv.status],
    tone: STATUS_TONE[inv.status],
    mood: mood(inv),
  }));

  return (
    <div className="hangar-page">
      <div className="hangar-stage">
        <Hangar items={items} onOpen={(id) => router.push(`/investigations/${id}`)} motion={!reduced} />
        <nav className="hangar-top" aria-label="TrustGate">
          <Link href="/" className="brand">
            <span className="brand-mark">
              <Icon name="shield" size={15} />
            </span>
            TrustGate
          </Link>
          <span className="chip">Approval-gated repairs for Flagsmith-controlled AI agents</span>
          {demo && <span className="demo-chip">Demo data</span>}
        </nav>
        <div className="hangar-copy">
          <h1>When an AI agent breaks after a flag release</h1>
          <p>
            TrustGate replays it in a sandbox, finds the one flag change that caused it, and changes
            nothing in Flagsmith until you approve the fix. Pick an agent to see its incident.
          </p>
        </div>
      </div>

      <section className="list" aria-label="Incidents">
        <h2>Incidents</h2>
        {!data ? (
          error ? (
            <p className="error">Could not reach the API: {error}</p>
          ) : (
            <div style={{ display: "grid", gap: 8 }} aria-busy="true" aria-label="Loading incidents">
              <div className="skeleton" />
              <div className="skeleton" />
            </div>
          )
        ) : data.length === 0 ? (
          <p className="muted">No incidents yet. They appear here when an agent alert starts one.</p>
        ) : (
          <ul className="rows">
            {data.map((inv) => (
              <li key={inv.id}>
                <Link href={`/investigations/${inv.id}`} style={{ "--team": TEAM[inv.domain] } as React.CSSProperties}>
                  <span className="row-mark" aria-hidden />
                  <span>
                    <span className="row-title">{AGENT[inv.domain]}</span>
                    <span className="row-sub">
                      {" "}
                      · {summary(inv)}
                    </span>
                  </span>
                  <StatusBadge status={inv.status} />
                  <span className="row-time">{formatClock(inv.timestamps.alert)}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
