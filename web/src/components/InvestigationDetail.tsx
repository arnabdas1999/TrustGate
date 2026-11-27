"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { formatValue, percent } from "@/lib/format";
import { useActiveTime, useDemoMode, usePolling, useReducedMotion } from "@/lib/hooks";
import type { Investigation, InvestigationView, Proposal, TestResult } from "@/lib/types";
import type { GateState, PodStep } from "./arena/Arena";
import type { Mood } from "./arena/Robot";
import { ApprovePanel } from "./ApprovePanel";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { Icon } from "./Icon";
import { StatusBadge } from "./StatusBadge";
import { StatusBanner } from "./StatusBanner";
import { Timeline } from "./Timeline";

// WebGL only exists in the browser: the scene is client-only and loads after the page.
const Arena = dynamic(() => import("./arena/Arena"), {
  ssr: false,
  loading: () => <div className="arena arena-loading" aria-hidden />,
});

export const TEAM = { opspilot: "#f59e0b", vaultguard: "#14b8a6" } as const;
export const AGENT = { opspilot: "OpsPilot", vaultguard: "VaultGuard" } as const;

const PHASE: Record<TestResult["phase"], string> = {
  reproduce: "Reproduce",
  search: "Search",
  escalate: "More runs",
  confirm: "Confirm",
  validate: "Validate repair",
  post_write: "After write",
};

function moodFor(inv: Investigation): Mood {
  switch (inv.status) {
    case "capturing":
    case "reproducing":
    case "searching":
    case "validating":
      return "search";
    case "proposed":
    case "approved":
    case "applied":
      return "alert";
    case "verified":
      return "happy";
    case "failed":
    case "stale":
    case "inconclusive":
    case "not_reproducible":
      return "sad";
    default:
      return "idle";
  }
}

function gateFor(proposal: Proposal | null, approvedLocally: boolean): GateState {
  if (!proposal) return "none";
  switch (proposal.state) {
    case "proposed":
      return approvedLocally ? "opening" : "locked";
    case "approved":
    case "applied":
      return "opening";
    case "verified":
      return "open";
    case "failed":
      return "breached";
    default:
      return "sealed";
  }
}

function headline(inv: Investigation) {
  const agent = AGENT[inv.domain];
  const culprit = inv.culprit_set[0];
  if (culprit && inv.culprit_set.length === 1)
    return (
      <>
        <code>{culprit.flag}</code> broke {agent}
      </>
    );
  if (inv.culprit_set.length > 1) return <>{inv.culprit_set.length} flag changes together broke {agent}</>;
  if (["capturing", "reproducing", "searching", "validating"].includes(inv.status))
    return <>Looking for the flag change that broke {agent}</>;
  return <>{agent} incident</>;
}

function checks(inv: Investigation, proposal: Proposal | null): string[] {
  const out: string[] = [];
  const repro = inv.steps.find((s) => s.phase === "reproduce" && s.outcome === "FAIL");
  if (repro) out.push(`Reproduced: ${repro.failures} of ${repro.runs} runs failed`);
  const confirm = inv.steps.find((s) => s.phase === "confirm" && s.outcome === "FAIL");
  if (confirm && inv.culprit_set.length)
    out.push(`Alone, ${inv.culprit_set.map((c) => c.flag).join(" + ")} fails ${confirm.failures} of ${confirm.runs}`);
  const c = proposal?.candidate;
  if (c?.validation) out.push(`With the fix: ${c.validation.failures} of ${c.validation.runs} runs failed`);
  if (c && c.heldout_passes !== null && c.heldout_runs !== null)
    out.push(`${c.heldout_passes} of ${c.heldout_runs} other cases still pass`);
  if (c?.cost_kept !== null && c?.cost_kept !== undefined) out.push(`Keeps ${percent(c.cost_kept)} of the release's saving`);
  return out;
}

function Hud({ view, refresh }: { view: InvestigationView; refresh: () => void }) {
  const { investigation: inv, proposal, calls, rollback_of, active_ms } = view;
  const { data: audit } = usePolling(api.audit, 2000);
  const reduced = useReducedMotion();
  const demo = useDemoMode();
  const [selected, setSelected] = useState<number | null>(null);
  const [drawer, setDrawer] = useState(false);
  const [approvedFor, setApprovedFor] = useState<string | null>(null);
  const closeDrawer = useCallback(() => setDrawer(false), []);

  const culpritFlags = useMemo(() => new Set(inv.culprit_set.map((c) => c.flag)), [inv.culprit_set]);
  const pods: PodStep[] = useMemo(
    () =>
      inv.steps.map((s) => ({
        label: PHASE[s.phase],
        runs: s.runs,
        failures: s.failures,
        outcome: s.outcome,
        flags: s.changes.map((c) => c.flag),
        hasCulprit: s.changes.some((c) => culpritFlags.has(c.flag)),
      })),
    [inv.steps, culpritFlags],
  );
  const gate = gateFor(proposal, approvedFor === proposal?.id);
  const op = proposal?.candidate.ops[0];
  const step = selected !== null ? inv.steps[selected] : null;
  const facts = checks(inv, proposal);
  const runShare = Math.min(1, inv.runs_used / inv.budget_runs);

  return (
    <div className="stage-page">
      <Arena
        team={TEAM[inv.domain]}
        agentName={AGENT[inv.domain]}
        environment={op?.environment ?? `${AGENT[inv.domain]} Prod`}
        steps={pods}
        culprit={inv.culprit_set[0]?.flag ?? null}
        mood={gate === "open" ? "happy" : moodFor(inv)}
        gate={gate}
        selected={selected}
        onSelect={setSelected}
        motion={!reduced}
      />

      <div className="hud-top">
        <Link href="/" className="hud-back">
          <Icon name="arrowLeft" size={14} />
          Inbox
        </Link>
        <div className="hud-title">
          <span className="hud-agent" style={{ "--team": TEAM[inv.domain] } as React.CSSProperties}>
            {AGENT[inv.domain]}
          </span>
          <span className="hud-id num">{inv.id}</span>
        </div>
        <StatusBadge status={inv.status} />
        {demo && <span className="demo-chip">Demo data</span>}
        <div className="hud-runs" title={`${inv.runs_used} of ${inv.budget_runs} runs used`}>
          <span className="num">
            {inv.runs_used}/{inv.budget_runs} runs
          </span>
          <span className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={inv.budget_runs} aria-valuenow={inv.runs_used} aria-label="Run budget used">
            <span style={{ transform: `scaleX(${runShare})` }} />
          </span>
        </div>
        <button className="hud-btn" onClick={() => setDrawer(true)}>
          Evidence
        </button>
      </div>

      {step && (
        <div className="hud-card step-card" role="status">
          <div className="step-head">
            <strong>{PHASE[step.phase]}</strong>
            <span className={`outcome outcome-${step.outcome}`}>{step.outcome.toLowerCase()}</span>
          </div>
          <p className="num">
            {step.failures} of {step.runs} runs failed
          </p>
          <p className="step-flags">
            {step.changes.length === 0
              ? "Baseline: none of the release changes applied"
              : step.changes.map((c) => (
                  <code key={c.flag} className={culpritFlags.has(c.flag) ? "culprit" : undefined}>
                    {c.flag}
                  </code>
                ))}
          </p>
        </div>
      )}

      <section className="hud-card story" aria-label="What happened">
        <p className="story-meta">
          {AGENT[inv.domain]} agent{inv.segment && <> · segment <code>{inv.segment}</code></>}
        </p>
        <h1 className="story-headline">{headline(inv)}</h1>
        {op && (
          <p className="story-fix">
            Fix: <code>{op.feature}</code> → <strong>{formatValue(op.value)}</strong>{" "}
            {op.segment ? <>for {op.segment} only</> : <>for everyone</>}
          </p>
        )}
        <StatusBanner status={inv.status} hasRollback={rollback_of !== null} />
        {facts.length > 0 && (
          <ul className="facts">
            {facts.map((f) => (
              <li key={f}>
                <Icon name="check" size={14} />
                {f}
              </li>
            ))}
          </ul>
        )}
        <Timeline timestamps={inv.timestamps} />
      </section>

      {proposal && (
        <ApprovePanel
          investigation={inv}
          proposal={proposal}
          rollbackOf={rollback_of ?? null}
          onApproved={() => setApprovedFor(proposal.id)}
          onChanged={refresh}
        />
      )}

      <p className="hud-hint" aria-hidden>
        Drag to look around · click a pod
      </p>

      <EvidenceDrawer
        open={drawer}
        onClose={closeDrawer}
        investigation={inv}
        proposal={proposal}
        calls={calls ?? []}
        audit={audit ?? []}
        activeMs={active_ms}
      />
    </div>
  );
}

export function InvestigationDetail() {
  const { id } = useParams<{ id: string }>();
  const loadView = useCallback(() => api.investigation(id), [id]);
  const { data: view, error, refresh } = usePolling(loadView, 1000);
  useActiveTime(id);

  if (!view) {
    return (
      <div className="stage-page">
        <div className="arena arena-loading" aria-hidden />
        <p className={`hud-loading${error ? " error" : ""}`}>{error ? `Could not load: ${error}` : "Loading the investigation…"}</p>
      </div>
    );
  }
  return <Hud view={view} refresh={() => void refresh()} />;
}
