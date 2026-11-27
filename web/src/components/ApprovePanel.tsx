"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { formatValue } from "@/lib/format";
import { useApproverSession, useNow } from "@/lib/hooks";
import { releaseValue } from "@/lib/proposal";
import type { Investigation, Proposal } from "@/lib/types";
import { Icon } from "./Icon";

const DONE_COPY: Record<string, string> = {
  approved: "Approved. The gate is writing the change.",
  applied: "Written. Reading back what Flagsmith serves.",
  verified: "Verified. Flagsmith serves the fix.",
  rejected: "Rejected. Nothing was written.",
  expired: "Expired. Nothing was written.",
  stale: "Refused: Flagsmith changed since the proposal.",
  failed: "Not confirmed by the read-back.",
};

function countdown(ms: number): string {
  if (ms <= 0) return "expired";
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The one place an action lives: the exact change as a diff, then sign in, approve or reject. */
export function ApprovePanel({
  investigation,
  proposal,
  rollbackOf,
  onApproved,
  onChanged,
}: {
  investigation: Investigation;
  proposal: Proposal;
  rollbackOf: string | null;
  onApproved: () => void;
  onChanged: () => void;
}) {
  const { session, login, logout } = useApproverSession();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"login" | "approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const now = useNow();
  const waiting = proposal.state === "proposed";
  const left = new Date(proposal.expires_at).getTime() - now;

  async function run(kind: "login" | "approve" | "reject", action: () => Promise<unknown>) {
    setBusy(kind);
    setError(null);
    try {
      await action();
      if (kind === "approve") onApproved();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const title = rollbackOf
    ? waiting
      ? "Approve the rollback"
      : "Rollback"
    : waiting
      ? "Approve this change"
      : "The change";

  return (
    <section className={`hud-card approve${waiting ? " approve-waiting" : ""}`} aria-label="Approval">
      <header className="approve-head">
        <h2>
          <Icon name={rollbackOf ? "undo" : "shield"} size={16} />
          {title}
        </h2>
        {waiting && now > 0 && <span className="countdown num">{countdown(left)}</span>}
      </header>

      {rollbackOf && (
        <p className="approve-note">
          Undoes <code>{rollbackOf}</code>, which was written but not confirmed. It needs its own approval.
        </p>
      )}

      <div className="diffs">
        {proposal.candidate.ops.map((op, i) => {
          const before = op.tool === "delete_feature_segment" ? op.value : releaseValue(investigation, op.feature);
          const after = op.tool === "delete_feature_segment" ? null : op.value;
          return (
            <div key={i} className="diff-card">
              <div className="diff-meta">
                <strong>{op.feature}</strong>
                <span>{op.segment ? `segment ${op.segment}` : "everyone"} · {op.environment}</span>
              </div>
              <pre className="diff">
                {before !== undefined && (
                  <div className="del">
                    <span>−</span>
                    <span>{formatValue(before)}</span>
                  </div>
                )}
                <div className="ins">
                  <span>+</span>
                  <span>{after === null ? "(override removed)" : formatValue(after)}</span>
                </div>
              </pre>
            </div>
          );
        })}
      </div>

      {waiting && !session && (
        <form
          className="approve-form"
          onSubmit={(e) => {
            e.preventDefault();
            void run("login", async () => {
              await login(password);
              setPassword("");
            });
          }}
        >
          <label htmlFor="approver-password">Approver password</label>
          <div className="approve-row">
            <input
              id="approver-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
            <button type="submit" className="primary" disabled={busy !== null || !password}>
              <span className="label">{busy === "login" ? "Signing in…" : "Sign in"}</span>
            </button>
          </div>
        </form>
      )}

      {waiting && session && (
        <div className="approve-actions">
          <button
            className={`primary approve-btn${busy === "approve" ? " busy" : ""}`}
            disabled={busy !== null}
            onClick={() => void run("approve", () => api.approve(proposal.id, session.token))}
          >
            <Icon name="check" />
            <span className="label">{busy === "approve" ? "Opening the gate…" : "Approve and apply"}</span>
          </button>
          <span className="spacer" />
          <button
            className="reject"
            disabled={busy !== null}
            onClick={() => void run("reject", () => api.reject(proposal.id, session.token))}
          >
            Reject
          </button>
        </div>
      )}

      {waiting && session && (
        <p className="signed-as">
          Signed in as {session.approver} ·{" "}
          <button className="ghost" onClick={logout}>
            sign out
          </button>
        </p>
      )}

      {!waiting && (
        <p className={`approve-done approve-done-${proposal.state}`} role="status">
          <Icon name={proposal.state === "verified" ? "checkCircle" : proposal.state === "failed" || proposal.state === "stale" ? "x" : "info"} />
          {DONE_COPY[proposal.state] ?? proposal.state}
        </p>
      )}

      {error && (
        <p className="approve-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
