// Types shared by the API server and the web UI.

export type ScenarioState =
  | "absent" // no workloads deployed
  | "settling" // deployed, not yet in its designed state
  | "reproduced"; // every check passes: ready to investigate

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

export interface ScenarioStatus {
  id: string;
  namespace: string;
  state: ScenarioState;
  checks: CheckResult[];
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
}

export interface ClusterHealth {
  reachable: boolean;
  message?: string;
}
