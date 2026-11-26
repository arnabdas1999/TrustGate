import { OutcomeBadge } from "./StatusBadge";
import type { FlagChange, TestResult } from "@/lib/types";

const PHASE_LABEL: Record<TestResult["phase"], string> = {
  reproduce: "Reproduce",
  search: "Search",
  escalate: "More runs",
  confirm: "Confirm",
  validate: "Validate repair",
  post_write: "After write",
};

/** One row per tested configuration; a dot marks each release change that row applied. */
export function SearchMatrix({
  suspects,
  steps,
  culprits,
}: {
  suspects: FlagChange[];
  steps: TestResult[];
  culprits: FlagChange[];
}) {
  const culpritFlags = new Set(culprits.map((c) => c.flag));
  const searchRuns = steps
    .filter((s) => s.phase === "search" || s.phase === "escalate")
    .reduce((sum, s) => sum + s.runs, 0);
  const exhaustive = 2 ** suspects.length * 3;

  return (
    <div>
      <div className="table-wrap">
        <table className="matrix">
          <thead>
            <tr>
              <th>Phase</th>
              {suspects.map((s) => (
                <th key={s.flag} className={culpritFlags.has(s.flag) ? "culprit" : undefined}>
                  <code>{s.flag}</code>
                </th>
              ))}
              <th>Runs</th>
              <th>Failures</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((step, i) => {
              const applied = new Set(step.changes.map((c) => c.flag));
              const override = Object.keys(step.config.segment_overrides).length > 0;
              return (
                <tr key={i}>
                  <td>
                    {PHASE_LABEL[step.phase]}
                    {override && <span className="muted"> + segment override</span>}
                    {!step.fresh && <span className="muted"> (cached)</span>}
                  </td>
                  {suspects.map((s) => (
                    <td key={s.flag} className="dot-cell">
                      {applied.has(s.flag) ? "●" : ""}
                    </td>
                  ))}
                  <td>{step.runs}</td>
                  <td>{step.failures}</td>
                  <td>
                    <OutcomeBadge outcome={step.outcome} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {suspects.length > 0 && (
        <p className="muted small">
          {searchRuns} search runs so far. Trying every combination would take{" "}
          {2 ** suspects.length} configurations × 3 = {exhaustive} runs.
        </p>
      )}
    </div>
  );
}
