"use client";

import { useEffect, useRef, useState } from "react";
import { KIND_LABEL, durationMs, evidenceLine, formatDuration, formatValue, percent } from "@/lib/format";
import { chosenCandidateIndex, describeCandidate } from "@/lib/proposal";
import type { AuditEntry, Investigation, McpCall, Proposal } from "@/lib/types";
import { Icon } from "./Icon";
import { SearchMatrix } from "./SearchMatrix";
import { ToolCallLog } from "./ToolCallLog";

/** Everything behind the scene, for the reviewer who wants the numbers: a slide-over panel. */
export function EvidenceDrawer({
  open,
  onClose,
  investigation: inv,
  proposal,
  calls,
  audit,
  activeMs,
}: {
  open: boolean;
  onClose: () => void;
  investigation: Investigation;
  proposal: Proposal | null;
  calls: McpCall[];
  audit: AuditEntry[];
  activeMs: number;
}) {
  const panel = useRef<HTMLElement>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const chosen = proposal?.candidate;
  const chosenIndex = chosen ? chosenCandidateIndex(inv.candidates, chosen) : -1;
  const validation = chosen?.validation;

  return (
    <>
      <div className={`scrim${open ? " scrim-open" : ""}`} onClick={onClose} aria-hidden />
      <aside
        ref={panel}
        className={`drawer${open ? " drawer-open" : ""}`}
        aria-label="Evidence"
        aria-hidden={!open}
        tabIndex={-1}
        inert={!open}
      >
        <header className="drawer-head">
          <h2>Evidence</h2>
          <button className="ghost icon-btn" onClick={onClose} aria-label="Close evidence">
            <Icon name="x" />
          </button>
        </header>

        <div className="drawer-body">
          {validation && (
            <p className="evidence-line">
              <Icon name="checkCircle" />
              {evidenceLine(validation.runs, validation.failures, validation.distinct_behaviours)}
            </p>
          )}

          <section>
            <h3>How the search narrowed it down</h3>
            <SearchMatrix suspects={inv.suspects} steps={inv.steps} culprits={inv.culprit_set} />
          </section>

          {inv.candidates.length > 0 && (
            <section>
              <h3>Repairs tested, narrowest first</h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Option</th>
                      <th scope="col">Change</th>
                      <th scope="col">Held-out</th>
                      <th scope="col">Saving kept</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inv.candidates.map((c, i) => (
                      <tr key={i} className={i === chosenIndex ? "chosen" : undefined}>
                        <td>
                          {KIND_LABEL[c.kind]}
                          {i === chosenIndex && <span className="chosen-tag">proposed</span>}
                        </td>
                        <td>{describeCandidate(c, inv)}</td>
                        <td className="num">
                          {c.heldout_passes ?? "—"}/{c.heldout_runs ?? "—"}
                        </td>
                        <td className="num">{percent(c.cost_kept)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {calls.length > 0 && (
            <section>
              <h3>The exact call the gate sends</h3>
              <div className="call">
                <button
                  className="ghost copy"
                  onClick={() => {
                    void navigator.clipboard?.writeText(JSON.stringify(calls, null, 2));
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1400);
                  }}
                >
                  <Icon name={copied ? "check" : "copy"} size={14} />
                  {copied ? "Copied" : "Copy"}
                </button>
                <pre>{JSON.stringify(calls, null, 2)}</pre>
              </div>
            </section>
          )}

          {proposal && proposal.readback.length > 0 && (
            <section>
              <h3>What Flagsmith serves now</h3>
              <ul className="readback">
                {proposal.readback.map((r, i) => (
                  <li key={i}>
                    <code>{r.identity.identifier}</code>
                    {Object.entries(r.values).map(([flag, value]) => (
                      <span key={flag}>
                        {flag} = <strong>{formatValue(value)}</strong>
                      </span>
                    ))}
                    {r.from_default_handler && <span className="error">default handler: not verified</span>}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section>
            <h3>Gate activity</h3>
            <ToolCallLog entries={audit} />
          </section>

          <dl className="drawer-stats">
            <div>
              <dt>Time to verified fix</dt>
              <dd className="num">{formatDuration(durationMs(inv.timestamps.alert, inv.timestamps.verified))}</dd>
            </div>
            <div>
              <dt>Your time</dt>
              <dd className="num">{formatDuration(activeMs)}</dd>
            </div>
            <div>
              <dt>Runs</dt>
              <dd className="num">
                {inv.runs_used}/{inv.budget_runs}
              </dd>
            </div>
          </dl>
        </div>
      </aside>
    </>
  );
}
