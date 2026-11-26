"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import {
  KIND_LABEL,
  describeOp,
  durationMs,
  evidenceLine,
  formatDuration,
  formatValue,
  percent,
} from "@/lib/format";
import { useApproverSession } from "@/lib/hooks";
import type { Investigation, Proposal, RepairCandidate } from "@/lib/types";

function sameCandidate(a: RepairCandidate, b: RepairCandidate) {
  return a.kind === b.kind && JSON.stringify(a.ops) === JSON.stringify(b.ops);
}

export function ProposalCard({
  investigation,
  proposal,
  activeMs,
  onChanged,
}: {
  investigation: Investigation;
  proposal: Proposal;
  activeMs: number;
  onChanged: () => void;
}) {
  const { session, login, logout } = useApproverSession();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chosen = proposal.candidate;
  const validation = chosen.validation;
  const waiting = proposal.state === "proposed";

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`card proposal ${waiting ? "proposal-waiting" : ""}`}>
      <header className="proposal-head">
        <h2>{waiting ? "Needs your approval" : `Proposal ${proposal.state}`}</h2>
        <span className="muted small">payload {proposal.payload_hash.slice(0, 12)}</span>
      </header>

      <p>{proposal.explanation}</p>

      {validation && (
        <p className="evidence">
          <strong>Evidence:</strong> {evidenceLine(validation.runs, validation.failures)}
        </p>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Option</th>
              <th>Flagsmith change</th>
              <th>Violations</th>
              <th>Held-out passes</th>
              <th>Cost saving kept</th>
            </tr>
          </thead>
          <tbody>
            {investigation.candidates.map((c, i) => (
              <tr key={i} className={sameCandidate(c, chosen) ? "chosen" : undefined}>
                <td>
                  {KIND_LABEL[c.kind]}
                  {sameCandidate(c, chosen) && <span className="badge badge-good">proposed</span>}
                </td>
                <td>
                  {c.ops.length === 1 ? describeOp(c.ops[0]) : `${c.ops.length} flags reverted`}
                </td>
                <td>{c.violations ?? "—"}</td>
                <td>
                  {c.heldout_passes ?? "—"} / {c.heldout_runs ?? "—"}
                  {c.baseline_heldout_passes !== null && (
                    <span className="muted"> (baseline {c.baseline_heldout_passes})</span>
                  )}
                </td>
                <td>{percent(c.cost_kept)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>The exact change it will make</h3>
      <pre className="code">{JSON.stringify(chosen.ops, null, 2)}</pre>

      {waiting && !session && (
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await login(password);
              setPassword("");
            });
          }}
        >
          <input
            type="password"
            placeholder="Approver password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
          <button type="submit" disabled={busy || !password}>
            Sign in to approve
          </button>
        </form>
      )}

      {waiting && session && (
        <div className="row">
          <button
            className="primary"
            disabled={busy}
            onClick={() => void run(() => api.approve(proposal.id, session.token))}
          >
            Approve and apply
          </button>
          <button disabled={busy} onClick={() => void run(() => api.reject(proposal.id, session.token))}>
            Reject
          </button>
          <span className="muted small">
            Signed in as {session.approver} ·{" "}
            <button className="link" onClick={logout}>
              sign out
            </button>
          </span>
        </div>
      )}

      {error && <p className="error">{error}</p>}

      {proposal.readback.length > 0 && (
        <>
          <h3>What Flagsmith serves now</h3>
          <ul className="readback">
            {proposal.readback.map((r, i) => (
              <li key={i}>
                <code>{r.identity.identifier}</code>{" "}
                <span className="muted">{JSON.stringify(r.identity.traits)}</span> →{" "}
                {Object.entries(r.values).map(([flag, value]) => (
                  <span key={flag}>
                    <code>{flag}</code> = <strong>{formatValue(value)}</strong>{" "}
                  </span>
                ))}
                {r.from_default_handler && (
                  <span className="badge badge-bad">default handler: not verified</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="stats">
        <div>
          <span className="muted small">Time to verified fix</span>
          <strong>
            {formatDuration(durationMs(investigation.timestamps.alert, investigation.timestamps.verified))}
          </strong>
        </div>
        <div>
          <span className="muted small">Your time</span>
          <strong>{formatDuration(activeMs)}</strong>
        </div>
        <div>
          <span className="muted small">Expires</span>
          <strong>{new Date(proposal.expires_at).toLocaleTimeString()}</strong>
        </div>
      </div>
    </section>
  );
}
