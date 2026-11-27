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
import { Icon } from "./Icon";
import type { Investigation, McpCall, Proposal, RepairCandidate } from "@/lib/types";

/** Real candidates from the search carry no ops (only the chosen one is built), so describe
 * them from the culprit set and segment when ops are missing. */
function describeCandidate(c: RepairCandidate, inv: Investigation): string {
  if (c.kind === "global") return `all ${inv.suspects.length} changed flags reverted`;
  if (c.ops.length === 1) return describeOp(c.ops[0]);
  if (c.ops.length > 1) return `${c.ops.length} flags changed`;
  const culprits = inv.culprit_set.map((f) => f.flag).join(", ") || "the culprit flags";
  switch (c.kind) {
    case "segment_override":
      return `${culprits} back to baseline for segment ${inv.segment ?? "?"}`;
    case "surgical":
      return `${culprits} back to baseline for everyone`;
  }
}

/** Flags a candidate touches, as a comparable key ("" when the search gave no ops). */
function opsKey(c: RepairCandidate): string {
  return c.ops
    .map((op) => `${op.feature}@${op.segment ?? ""}`)
    .sort()
    .join(",");
}

/** Which row is the proposed one. Per-flag surgical options share a kind, so match the flags
 * too (code rebuilds the chosen ops, so compare feature and segment, not the whole op). */
function chosenCandidateIndex(candidates: RepairCandidate[], chosen: RepairCandidate): number {
  const key = opsKey(chosen);
  const exact = candidates.findIndex((c) => c.kind === chosen.kind && opsKey(c) === key);
  if (exact >= 0) return exact;
  const bare = candidates.findIndex((c) => c.kind === chosen.kind && c.ops.length === 0);
  return bare >= 0 ? bare : candidates.findIndex((c) => c.kind === chosen.kind);
}

export function ProposalCard({
  investigation,
  proposal,
  calls,
  rollbackOf,
  activeMs,
  onChanged,
}: {
  investigation: Investigation;
  proposal: Proposal;
  calls: McpCall[];
  rollbackOf: string | null;
  activeMs: number;
  onChanged: () => void;
}) {
  const { session, login, logout } = useApproverSession();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showOps, setShowOps] = useState(false);

  const chosen = proposal.candidate;
  const chosenIndex = chosenCandidateIndex(investigation.candidates, chosen);
  const validation = rollbackOf ? null : chosen.validation;
  const waiting = proposal.state === "proposed";
  const title = rollbackOf
    ? waiting
      ? "Rollback needs your approval"
      : `Rollback ${proposal.state}`
    : waiting
      ? "Needs your approval"
      : `Proposal ${proposal.state}`;

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
        <h2>
          <Icon name={rollbackOf ? "undo" : waiting ? "alert" : "shield"} size={18} />
          {title}
        </h2>
        <span className="payload" title={proposal.payload_hash}>
          payload {proposal.payload_hash.slice(0, 12)}
        </span>
      </header>

      {rollbackOf && (
        <p className="banner banner-warn">
          <Icon name="undo" />
          <span>
          This undoes repair <code>{rollbackOf}</code>, which was written but not confirmed by the
          read-back. It is a separate change and needs its own approval.
          </span>
        </p>
      )}

      <p className="explanation">{proposal.explanation}</p>

      {validation && (
        <p className="evidence">
          <Icon name="check" />
          <span>
            <strong>Evidence:</strong>{" "}
            {evidenceLine(validation.runs, validation.failures, validation.distinct_behaviours)}
          </span>
        </p>
      )}

      {!rollbackOf && investigation.candidates.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Option</th>
                <th scope="col">Flagsmith change</th>
                <th scope="col">Violations</th>
                <th scope="col">Held-out passes</th>
                <th scope="col">Cost saving kept</th>
              </tr>
            </thead>
            <tbody>
              {investigation.candidates.map((c, i) => {
                const isChosen = i === chosenIndex;
                return (
                  <tr key={i} className={isChosen ? "chosen" : undefined}>
                    <td>
                      <span className="option-name">
                        {KIND_LABEL[c.kind]}
                        {isChosen && (
                          <span className="badge badge-good">
                            <span className="badge-dot" aria-hidden />
                            Proposed
                          </span>
                        )}
                      </span>
                    </td>
                    <td>{describeCandidate(c, investigation)}</td>
                    <td className="num">{c.violations ?? "—"}</td>
                    <td className="num">
                      {c.heldout_passes ?? "—"} / {c.heldout_runs ?? "—"}
                      {c.baseline_heldout_passes !== null && (
                        <span className="muted"> (baseline {c.baseline_heldout_passes})</span>
                      )}
                    </td>
                    <td className="num">{percent(c.cost_kept)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="section-label">
        <Icon name="terminal" />
        The exact change Flagsmith will receive
      </h3>
      {calls.length > 0 ? (
        <>
          <p className="muted small">
            {calls.length === 1 ? "One call" : `${calls.length} calls`} the gate will send, only
            after approval. The approval id is filled in when you approve.
          </p>
          <div className="code-wrap">
            <div className="code-bar">
              <span>Flagsmith MCP</span>
              <span>
                <Icon name="lock" size={12} /> sent by the gate after approval
              </span>
            </div>
            <pre className="code">{JSON.stringify(calls, null, 2)}</pre>
          </div>
        </>
      ) : (
        <p className="muted small">The gate has not rendered the call yet.</p>
      )}
      <button className="link small" onClick={() => setShowOps((v) => !v)}>
        {showOps ? "Hide" : "Show"} TrustGate&apos;s change (what was validated)
      </button>
      {showOps && (
        <ul className="ops">
          {chosen.ops.map((op, i) => (
            <li key={i}>
              <code>{op.tool}</code> {describeOp(op)}
            </li>
          ))}
        </ul>
      )}

      {waiting && !session && (
        <form
          className="row approve-bar"
          style={{ alignItems: "flex-end" }}
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await login(password);
              setPassword("");
            });
          }}
        >
          <label className="field">
            Approver password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          <button type="submit" disabled={busy || !password}>
            <Icon name="lock" />
            Sign in to approve
          </button>
        </form>
      )}

      {waiting && session && (
        <div className="row approve-bar">
          <button
            className="primary"
            disabled={busy}
            onClick={() => void run(() => api.approve(proposal.id, session.token))}
          >
            <Icon name="check" />
            Approve and apply
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() => void run(() => api.reject(proposal.id, session.token))}
          >
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

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {proposal.readback.length > 0 && (
        <>
          <h3 className="section-label">
            <Icon name="check" />
            What Flagsmith serves now
          </h3>
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
