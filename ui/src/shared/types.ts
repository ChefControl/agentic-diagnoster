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
