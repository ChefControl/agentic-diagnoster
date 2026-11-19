import { ArrowUpRight } from "lucide-react";
import type { AgentRun } from "../shared/types.ts";
import { citedCalls, describeCall, headline, modelName } from "./answer.ts";

// The agent's answer, in plain words. Everything here comes from its report;
// the cited reads are ours (recorded by the agent's code), not the model's say-so.
export function AnswerCard({ run }: { run: AgentRun }) {
  const diagnosis = run.report.diagnosis;
  const reads = run.report.tool_calls.length;
  const fix = diagnosis?.next_action.proposed_fix;

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
