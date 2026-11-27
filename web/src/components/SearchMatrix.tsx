import type { FlagChange, TestOutcome, TestResult } from "@/lib/types";

const PHASE_LABEL: Record<TestResult["phase"], string> = {
  reproduce: "Reproduce",
  search: "Search",
  escalate: "More runs",
  confirm: "Confirm",
  validate: "Validate repair",
  post_write: "After write",
};

const OUTCOME_LABEL: Record<TestOutcome, string> = {
  PASS: "Pass",
  FAIL: "Fail",
  INCONCLUSIVE: "Inconclusive",
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
    <>
      <div className="table-wrap">
        <table className="matrix">
          <thead>
            <tr>
              <th scope="col">Step</th>
              {suspects.map((s) => (
                <th key={s.flag} scope="col" className={`flag-col${culpritFlags.has(s.flag) ? " culprit" : ""}`}>
                  <code>{s.flag.replace(/^agent_/, "")}</code>
                  {culpritFlags.has(s.flag) && <span className="sr-only"> (culprit)</span>}
                </th>
              ))}
              <th scope="col">Runs</th>
              <th scope="col">Failed</th>
              <th scope="col">Result</th>
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
                    {override && <span className="phase-note">with segment override</span>}
                    {!step.fresh && <span className="phase-note">cached</span>}
                  </td>
                  {suspects.map((s) => (
                    <td key={s.flag} className="dot-cell">
                      {applied.has(s.flag) && (
                        <>
                          <span className={`dot${culpritFlags.has(s.flag) ? " dot-culprit" : ""}`} aria-hidden />
                          <span className="sr-only">applied</span>
                        </>
                      )}
                    </td>
                  ))}
                  <td className="num">{step.runs}</td>
                  <td className="num">{step.failures}</td>
                  <td className={`outcome outcome-${step.outcome}`}>{OUTCOME_LABEL[step.outcome]}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {suspects.length > 0 && (
        <p className="footnote">
          {searchRuns} search runs so far. Trying every combination would take {2 ** suspects.length}{" "}
          configurations × 3 = {exhaustive} runs.
        </p>
      )}
    </>
  );
}
