import {
  ArrowUpRight,
  CircleAlert,
  CircleCheck,
  CircleSlash,
  CircleX,
  Clock,
  Cpu,
  Info,
  ListChecks,
  Search,
  ShieldAlert,
  Stethoscope,
  Wrench,
} from "lucide-react";
import type { ReactNode } from "react";
import type { AgentRemediation, AgentRun } from "../shared/types.ts";
import { citedCalls, describeCall, headline, modelName, remediationText } from "./answer.ts";

const ICON = { size: 16, strokeWidth: 1.75, "aria-hidden": true } as const;

// One icon per fix outcome, so the result reads before its sentence does.
const OUTCOME_ICON: Record<AgentRemediation["status"], typeof CircleCheck> = {
  fixed: CircleCheck,
  not_fixed: CircleX,
  stale: CircleAlert,
  failed: CircleX,
  denied: CircleSlash,
  not_allowed: CircleSlash,
  not_asked: Info,
  asked: Clock,
};

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
  const healthy = diagnosis?.root_cause.category === "none";
  const HeadIcon = !diagnosis ? CircleAlert : healthy ? CircleCheck : Stethoscope;
  const OutcomeIcon = OUTCOME_ICON[remediation?.status ?? "not_asked"];

  return (
    <article className="answer" aria-label="The agent's answer">
      <header className={`answer-head ${!diagnosis ? "is-stopped" : healthy ? "is-healthy" : "is-found"}`}>
        <span className="answer-head-icon">
          <HeadIcon size={18} strokeWidth={1.75} aria-hidden="true" />
        </span>
        <div className="answer-head-text">
          <span className="answer-kicker">{!diagnosis ? "No diagnosis" : "Root cause"}</span>
          <h2 className={`answer-headline ${diagnosis ? "" : "is-stopped"}`}>{headline(run)}</h2>
        </div>
      </header>

      {diagnosis && (
        <>
          <p className="answer-text answer-lead">{diagnosis.root_cause.explanation}</p>

          <h3 className="answer-section">
            <Search {...ICON} />
            What shows it
          </h3>
          <ol className="answer-evidence">
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
          </ol>

          {diagnosis.next_action.kind !== "none" && (
            <>
              <h3 className="answer-section">
                <ListChecks {...ICON} />
                What to do
              </h3>
              <p className="answer-text">{diagnosis.next_action.summary}</p>
              {fix && (
                <div className="answer-fix">
                  <p className="answer-fix-title">
                    <Wrench size={14} strokeWidth={1.75} aria-hidden="true" />
                    Proposed fix
                  </p>
                  <pre className="diff">
                    <span className="diff-meta">
                      {fix.target} <span className="diff-field">{fix.field}</span>
                    </span>
                    {"\n"}
                    <span className="diff-del">- {fix.from}</span>
                    {"\n"}
                    <span className="diff-add">+ {fix.to}</span>
                  </pre>
                </div>
              )}
            </>
          )}
        </>
      )}

      {children}

      {(remediation || unavailable) && (
        <div className={`answer-outcome is-${remediation?.status ?? "not_asked"}`} role="status">
          <OutcomeIcon className="answer-outcome-icon" size={18} strokeWidth={1.75} aria-hidden="true" />
          <div className="answer-outcome-body">
            <p>{remediationText(remediation ?? { status: "not_asked", detail: "", fix: null, expected_effect: null }, run.cluster)}</p>
            {remediation?.detail && <p className="answer-outcome-detail">{remediation.detail}</p>}
          </div>
        </div>
      )}

      <footer className="answer-meta">
        <span className="answer-meta-facts">
          <span className="meta-chip">
            <Cpu size={13} strokeWidth={1.75} aria-hidden="true" />
            {modelName(run.model)}
          </span>
          <span className="meta-chip">{run.cluster === "recorded" ? "recorded cluster" : "live cluster"}</span>
          <span className="meta-chip">
            {reads} cluster {reads === 1 ? "read" : "reads"}
          </span>
        </span>
        {run.trace_url && (
          <a href={run.trace_url} target="_blank" rel="noreferrer">
            View trace in Phoenix
            <ArrowUpRight size={14} strokeWidth={1.75} aria-label="opens Phoenix" />
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
      <CircleAlert size={18} strokeWidth={1.75} aria-hidden="true" />
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
      <div className="approval-head">
        <ShieldAlert className="approval-icon" size={20} strokeWidth={1.75} aria-hidden="true" />
        <div>
          <p className="approval-question">Apply this fix?</p>
          <p className="approval-note">
            The agent changes only <code>{fix.field}</code> on <code>{fix.target}</code>, then checks for up to 90
            seconds that the pods become ready. Read the diagnosis above before you approve.
          </p>
        </div>
      </div>
      {effect && (
        <p className="answer-text approval-effect">
          <span className="approval-label">Expected effect, in the agent's words</span>
          {effect}
        </p>
      )}
      {busy ? (
        <p className="answer-wait" role="status">
          <span className="answer-wait-dot" aria-hidden="true" />
          {busy === "approve" ? "Applying the fix and checking the result. This can take up to two minutes." : "Telling the agent."}
        </p>
      ) : (
        <div className="actions approval-actions">
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
