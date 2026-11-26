"use client";

import Link from "next/link";
import { useCallback } from "react";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { formatValue } from "@/lib/format";
import { useActiveTime, usePolling } from "@/lib/hooks";
import { ProposalCard } from "./ProposalCard";
import { SearchMatrix } from "./SearchMatrix";
import { StatusBadge } from "./StatusBadge";
import { Timeline } from "./Timeline";
import { ToolCallLog } from "./ToolCallLog";

export function InvestigationDetail() {
  const { id } = useParams<{ id: string }>();
  const loadView = useCallback(() => api.investigation(id), [id]);
  const { data: view, error, refresh } = usePolling(loadView, 1000);
  const { data: audit } = usePolling(api.audit, 2000);
  useActiveTime(id);

  if (!view) {
    return <p className="muted">{error ? `Could not load: ${error}` : "Loading…"}</p>;
  }
  const { investigation: inv, proposal, active_ms } = view;
  const budgetShare = Math.min(1, inv.runs_used / inv.budget_runs);

  return (
    <>
      <p>
        <Link href="/">← All investigations</Link>
      </p>
      <header className="page-head">
        <div>
          <h1>
            {inv.domain === "opspilot" ? "OpsPilot" : "VaultGuard"} incident{" "}
            <span className="muted small">{inv.id}</span>
          </h1>
          <p className="muted">
            Mode: {inv.mode === "guided" ? "LLM-guided search" : "plain ddmin"}
            {inv.segment && (
              <>
                {" "}
                · matched segment <code>{inv.segment}</code>
              </>
            )}
          </p>
        </div>
        <StatusBadge status={inv.status} />
      </header>

      <section className="card">
        <Timeline timestamps={inv.timestamps} />
        <div className="budget">
          <span className="muted small">
            Budget: {inv.runs_used} / {inv.budget_runs} runs · ${inv.usd_used.toFixed(2)} / $
            {inv.budget_usd.toFixed(2)}
          </span>
          <div className="bar">
            <div style={{ width: `${budgetShare * 100}%` }} />
          </div>
        </div>
      </section>

      {proposal && (
        <ProposalCard
          investigation={inv}
          proposal={proposal}
          activeMs={active_ms}
          onChanged={() => void refresh()}
        />
      )}

      <section className="card">
        <h2>Search</h2>
        {inv.culprit_set.length > 0 && (
          <p>
            Minimal failure-inducing set:{" "}
            {inv.culprit_set.map((c) => (
              <code key={c.flag}>
                {c.flag}: {formatValue(c.old)} → {formatValue(c.new)}
              </code>
            ))}
          </p>
        )}
        <SearchMatrix suspects={inv.suspects} steps={inv.steps} culprits={inv.culprit_set} />
      </section>

      <section className="card">
        <h2>Gate activity</h2>
        <ToolCallLog entries={audit ?? []} />
      </section>
    </>
  );
}
