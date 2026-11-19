import { ArrowUpRight } from "lucide-react";
import type { ReactNode } from "react";
import type { AgentRun } from "../shared/types.ts";
import { citedCalls, describeCall, headline, modelName, remediationText } from "./answer.ts";

// The agent's answer, in plain words. Everything here comes from its report;
// the cited reads are ours (recorded by the agent's code), not the model's say-so.
// `children` goes after the next action: the approval, while the agent waits for one.
export function AnswerCard({ run, children }: { run: AgentRun; children?: ReactNode }) {
  const diagnosis = run.report.diagnosis;
  const reads = run.report.tool_calls.length;
  const fix = diagnosis?.next_action.proposed_fix;
  // While the agent waits, the Approve box speaks for the fix; the outcome shows after.
  const remediation = run.report.remediation?.status === "asked" ? null : run.report.remediation;
  // On the recorded cluster the agent never asks, so say why there is no button.
  const unavailable = !remediation && fix && run.cluster === "recorded";

  return (
    <article className="answer" aria-label="The agent's answer">
      <h2 className={`answer-headline ${diagnosis ? "" : "is-stopped"}`}>{headline(run)}</h2>

      {diagnosis && (
        <>
          <p className="answer-text">{diagnosis.root_cause.explanation}</p>

          <h3 className="answer-section">What shows it</h3>
          <ul className="answer-evidence">
            {diagnosis.evidence.map((item, i) => (
              <li key={i}>
                <span className="answer-text">{item.fact}</span>
                <span className="answer-cites">
                  {citedCalls(run, item.tool_call_ids).map((call) => (
                    <span key={call.id} className="answer-cite" title={`${call.tool} ${JSON.stringify(call.args)}`}>
                      <span className="answer-cite-id">{call.id.replace("tc_", "#")}</span> {describeCall(call)}
                    </span>
                  ))}
                </span>
              </li>
            ))}
          </ul>

          {diagnosis.next_action.kind !== "none" && (
            <>
              <h3 className="answer-section">What to do</h3>
              <p className="answer-text">{diagnosis.next_action.summary}</p>
              {fix && (
                <p className="answer-fix">
                  Proposed fix: on <code>{fix.target}</code>, change <code>{fix.field}</code> from <code>{fix.from}</code>{" "}
                  to <code>{fix.to}</code>.
                </p>
              )}
            </>
          )}
        </>
      )}

      {children}

      {(remediation || unavailable) && (
        <div className={`answer-outcome is-${remediation?.status ?? "not_asked"}`} role="status">
          <p>{remediationText(remediation ?? { status: "not_asked", detail: "", fix: null, expected_effect: null }, run.cluster)}</p>
          {remediation?.detail && <p className="answer-outcome-detail">{remediation.detail}</p>}
        </div>
      )}

      <footer className="answer-meta">
        <span>
          {modelName(run.model)}, {run.cluster === "recorded" ? "recorded cluster" : "live cluster"}, {reads} cluster{" "}
          {reads === 1 ? "read" : "reads"}
        </span>
        {run.trace_url && (
          <a href={run.trace_url} target="_blank" rel="noreferrer">
            View trace in Phoenix
            <ArrowUpRight size={14} strokeWidth={1.5} aria-label="opens Phoenix" />
          </a>
        )}
      </footer>
    </article>
  );
}

export function Investigating() {
  return (
    <p className="answer-wait" role="status">
      <span className="answer-wait-dot" aria-hidden="true" />
      Investigating. The agent is reading the cluster; this usually takes under a minute.
    </p>
  );
}

export function NoAnswer({ error }: { error: string }) {
  return (
    <p className="answer-error" role="alert">
      No answer: {error}
    </p>
  );
}

// The person's decision on the agent's fix. Nothing changes until "Apply fix".
export function Approval({
  run,
  busy,
  error,
  onDecide,
}: {
  run: AgentRun;
  busy: "approve" | "deny" | null;
  error: string | null;
  onDecide: (approved: boolean) => void;
}) {
  const fix = run.report.diagnosis?.next_action.proposed_fix;
  const effect = run.report.remediation?.expected_effect;
  if (!fix) return null;
  return (
    <div className="approval" role="group" aria-label="Approve the fix">
      <p className="approval-question">Apply this fix?</p>
      {effect && (
        <p className="answer-text">
          <span className="approval-label">Expected effect, in the agent's words: </span>
          {effect}
        </p>
      )}
      <p className="approval-note">
        The agent changes only <code>{fix.field}</code> on <code>{fix.target}</code>, then checks for up to 90 seconds
        that the pods become ready. Read the diagnosis above before you approve.
      </p>
      {busy ? (
        <p className="answer-wait" role="status">
          <span className="answer-wait-dot" aria-hidden="true" />
          {busy === "approve" ? "Applying the fix and checking the result. This can take up to two minutes." : "Telling the agent."}
        </p>
      ) : (
        <div className="actions">
          <button className="btn btn-primary" onClick={() => onDecide(true)}>
            Apply fix
          </button>
          <button className="btn btn-quiet" onClick={() => onDecide(false)}>
            Don't apply
          </button>
        </div>
      )}
      {error && (
        <p className="action-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
