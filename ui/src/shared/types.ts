// Types shared by the API server and the web UI.

// The API refuses changes (POST, DELETE) without this header, which the UI
// sends. Other sites' pages can't send it, so they can't make changes.
export const REQUEST_HEADER = "x-requested-by";
export const REQUEST_HEADER_VALUE = "agentic-diagnoster";

export type ScenarioState =
  | "absent" // no workloads deployed
  | "settling" // deployed, not yet in its designed state
  | "reproduced" // every check passes: ready to investigate
  | "changed"; // someone changed it after deploy (e.g. the agent's approved fix): reset to restore

export interface ScenarioSummary {
  id: string;
  title: string; // reviewer-facing, e.g. "Missing config"
  namespace: string;
  question: string; // the only thing the agent is told
  fault: string; // reviewer-only, from the answer key
  category: string;
  remediable: boolean;
  state: ScenarioState;
}

export interface CheckResult {
  label: string;
  pass: boolean;
}

export interface PodSummary {
  name: string;
  ready: string; // "1/1"
  phase: string;
  reason: string | null; // e.g. CrashLoopBackOff
  restarts: number;
}

export interface EventSummary {
  type: string;
  reason: string;
  object: string;
  message: string;
  count: number;
  lastSeen: string | null;
}

// A deployed field that someone other than the scenario tooling has changed.
export interface FieldChange {
  resource: string; // e.g. deployment/payments-api
  field: string; // e.g. .spec.template.spec.containers[name="api"].readinessProbe.httpGet.path
  by: string; // the field manager that changed it, e.g. kubectl-patch
}

export interface ScenarioStatus {
  id: string;
  namespace: string;
  state: ScenarioState;
  checks: CheckResult[];
  changes: FieldChange[];
  pods: PodSummary[];
  events: EventSummary[];
}

// The scenario's answer key, from its expected.yaml. Shown to the reviewer
// (a white box), never sent to the agent.
export interface EvidenceSource {
  kind: string;
  name: string;
  aspect: string;
}

export interface EvidenceFact {
  fact: string;
  anyOf: EvidenceSource[];
}

export interface UnsafeAction {
  action: string;
  why: string;
}

export interface ScenarioSpec {
  fault: string;
  category: string; // one of scenarios/categories.yaml
  categoryDescription: string;
  resources: string[]; // naming any one is correct; empty when nothing is broken
  explanation: string;
  evidence: {
    required: EvidenceFact[]; // graded
    supporting: EvidenceFact[]; // shows thoroughness, not graded
  };
  safeActions: string[]; // any one of these is a correct fix
  unsafeActions: UnsafeAction[];
  remediation: {
    target: string;
    change: { field: string; from: string; to: string };
    verify: string[];
  } | null;
}

export interface PhoenixStatus {
  url: string | null; // null when PHOENIX_URL is not configured
  reachable: boolean;
  // The Playground with the agent's prompt and the eval dataset chosen; null until phoenix-setup has run.
  evalsUrl: string | null;
}

export interface ClusterHealth {
  reachable: boolean;
  message?: string;
}

// ---- the agent's answer (agent/src/diagnoster/contract.py) ----

export interface AgentEvidence {
  fact: string;
  tool_call_ids: string[];
}

export interface AgentProposedFix {
  target: string;
  field: string;
  from: string;
  to: string;
}

export interface AgentDiagnosis {
  root_cause: { category: string; resource: string | null; explanation: string };
  evidence: AgentEvidence[];
  next_action: { summary: string; kind: "none" | "recommend" | "remediate"; proposed_fix: AgentProposedFix | null };
}

export interface AgentToolCall {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  sources: { kind: string; name: string; aspect: string }[];
}

export type AgentStopReason =
  | "request_limit"
  | "tool_call_limit"
  | "token_limit"
  | "timeout"
  | "invalid_output"
  | "model_error";

// What became of the fix the agent asked to apply; written by the agent's code, not the model.
export interface AgentRemediation {
  status: "asked" | "not_asked" | "not_allowed" | "denied" | "stale" | "failed" | "fixed" | "not_fixed";
  detail: string;
  fix: AgentProposedFix | null;
  expected_effect: string | null; // the agent's words, given when it asked
}

export type AgentRunStatus = "complete" | "stopped" | "awaiting_approval";

export interface AgentReport {
  status: AgentRunStatus;
  stop_reason: AgentStopReason | null;
  diagnosis: AgentDiagnosis | null;
  tool_calls: AgentToolCall[];
  trace_id: string | null;
  remediation: AgentRemediation | null;
}

// One chat question, answered by the agent's POST /v1/runs.
export interface AgentRun {
  run_id: string; // with awaiting_approval, the run to approve or deny
  status: AgentRunStatus;
  model: string;
  cluster: "live" | "recorded";
  trace_url: string | null;
  report: AgentReport;
}

export interface AgentQuestion {
  question: string;
  session_id: string;
}
