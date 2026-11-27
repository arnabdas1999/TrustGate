"use client";

import Link from "next/link";
import { useCallback } from "react";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { formatValue } from "@/lib/format";
import { useActiveTime, usePolling } from "@/lib/hooks";
import { Icon } from "./Icon";
import { ProposalCard } from "./ProposalCard";
import { SearchMatrix } from "./SearchMatrix";
import { StatusBadge } from "./StatusBadge";
import { StatusBanner } from "./StatusBanner";
import { Timeline } from "./Timeline";
import { ToolCallLog } from "./ToolCallLog";

export function InvestigationDetail() {
  const { id } = useParams<{ id: string }>();
  const loadView = useCallback(() => api.investigation(id), [id]);
  const { data: view, error, refresh } = usePolling(loadView, 1000);
  const { data: audit } = usePolling(api.audit, 2000);
  useActiveTime(id);

  if (!view) {
    return error ? (
      <p className="error">Could not load: {error}</p>
    ) : (
      <div className="card" aria-busy="true" aria-label="Loading investigation" style={{ display: "grid", gap: 12 }}>
        <div className="skeleton" style={{ width: "40%" }} />
        <div className="skeleton" />
        <div className="skeleton" style={{ width: "70%" }} />
      </div>
    );
  }
  const { investigation: inv, proposal, calls, rollback_of, active_ms } = view;
  const budgetShare = Math.min(1, inv.runs_used / inv.budget_runs);
  const agent = inv.domain === "opspilot" ? "OpsPilot" : "VaultGuard";

  return (
    <>
      <Link href="/" className="back-link">
        <Icon name="arrowLeft" size={14} />
        All investigations
      </Link>
      <header className="page-head">
        <div>
          <div className="eyebrow">
            {agent} · <span className="mono">{inv.id}</span>
          </div>
          <h1>{agent} incident</h1>
          <p className="muted">
            {inv.mode === "guided" ? "LLM-guided search" : "Plain ddmin"}
            {inv.segment && (
              <>
                {" "}
                · matched segment <code>{inv.segment}</code>
              </>
            )}
          </p>
        </div>
        <StatusBadge status={inv.status} large />
      </header>

      <StatusBanner status={inv.status} hasRollback={rollback_of !== null} />

      <section className="card" aria-label="Progress">
        <Timeline timestamps={inv.timestamps} />
        <div className="budget">
          <div className="budget-head">
            <span>Run budget</span>
            <span className="num">
              {inv.runs_used} / {inv.budget_runs} runs · ${inv.usd_used.toFixed(2)} / ${inv.budget_usd.toFixed(2)}
            </span>
          </div>
          <div
            className="bar"
            role="progressbar"
            aria-label="Run budget used"
            aria-valuemin={0}
            aria-valuemax={inv.budget_runs}
            aria-valuenow={inv.runs_used}
          >
            <div style={{ width: `${budgetShare * 100}%` }} />
          </div>
        </div>
      </section>

      {proposal && (
        <ProposalCard
          investigation={inv}
          proposal={proposal}
          calls={calls ?? []}
          rollbackOf={rollback_of ?? null}
          activeMs={active_ms}
          onChanged={() => void refresh()}
        />
      )}

      <section className="card">
        <div className="card-head">
          <h2>
            <Icon name="search" />
            Search
          </h2>
        </div>
        {inv.culprit_set.length > 0 && (
          <div className="culprit-line" style={{ marginBottom: 16 }}>
            <span className="muted small">Minimal failure-inducing set</span>
            {inv.culprit_set.map((c) => (
              <span key={c.flag} className="culprit-chip">
                {c.flag}: <span className="from">{formatValue(c.old)}</span> →{" "}
                <span className="to">{formatValue(c.new)}</span>
              </span>
            ))}
          </div>
        )}
        <SearchMatrix suspects={inv.suspects} steps={inv.steps} culprits={inv.culprit_set} />
      </section>

      <section className="card">
        <div className="card-head">
          <h2>
            <Icon name="activity" />
            Gate activity
          </h2>
          <span className="muted small">Every Flagsmith call goes through the gate</span>
        </div>
        <ToolCallLog entries={audit ?? []} />
      </section>
    </>
  );
}
