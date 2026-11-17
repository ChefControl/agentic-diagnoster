import type { CoreV1Event, V1Deployment, V1EndpointSlice, V1Pod } from "@kubernetes/client-node";
import type { CheckResult, ScenarioState } from "../shared/types.ts";

// What the checks look at: one read of a scenario namespace.
export interface Snapshot {
  deployments: V1Deployment[];
  pods: V1Pod[];
  events: CoreV1Event[];
  endpointSlices: V1EndpointSlice[];
  logs: Record<string, string>; // by app label, current + previous container
}

interface Check {
  label: string;
  pass: (s: Snapshot) => boolean;
}

interface ScenarioChecks {
  logsFrom: string[]; // app labels whose logs the checks need
  checks: Check[];
}

// ---------------------------------------------------------------- helpers

const podsOf = (s: Snapshot, app: string) => s.pods.filter((p) => p.metadata?.labels?.app === app);

const isReady = (p: V1Pod) =>
  p.status?.conditions?.some((c) => c.type === "Ready" && c.status === "True") ?? false;

const restarts = (p: V1Pod) => p.status?.containerStatuses?.[0]?.restartCount ?? 0;

function available(s: Snapshot, name: string): boolean {
  const d = s.deployments.find((d) => d.metadata?.name === name);
  const want = d?.spec?.replicas ?? 1;
  return d !== undefined && (d.status?.availableReplicas ?? 0) >= want;
}

function endpointAddresses(s: Snapshot, service: string, readyOnly: boolean): string[] {
  return s.endpointSlices
    .filter((es) => es.metadata?.labels?.["kubernetes.io/service-name"] === service)
    .flatMap((es) => es.endpoints ?? [])
    .filter((e) => !readyOnly || e.conditions?.ready === true)
    .flatMap((e) => e.addresses);
}

// ---------------------------------------------------------------- per scenario
// Each list describes the scenario's designed state. All checks passing means
// the scenario reproduced and is ready for the agent to investigate.

export const SCENARIO_CHECKS: Record<string, ScenarioChecks> = {
  "missing-config": {
    logsFrom: ["checkout"],
    checks: [
      { label: "frontend is ready", pass: (s) => available(s, "frontend") },
      {
        label: "checkout container is restarting",
        pass: (s) => podsOf(s, "checkout").some((p) => restarts(p) >= 1),
      },
      {
        label: "checkout logs report DATABASE_URL missing",
        pass: (s) => (s.logs.checkout ?? "").includes("DATABASE_URL is not set"),
      },
    ],
  },
  "bad-probe": {
    logsFrom: [],
    checks: [
      {
        label: "payments-api pods are running",
        pass: (s) => {
          const pods = podsOf(s, "payments-api");
          return pods.length > 0 && pods.every((p) => p.status?.phase === "Running");
        },
      },
      {
        label: "readiness probe fails with 404",
        pass: (s) => {
          // Only events about pods that exist now, never a previous run's.
          // Require the designed 404, not a "connection refused" startup blip.
          const current = new Set(podsOf(s, "payments-api").map((p) => p.metadata?.name));
          return s.events.some(
            (e) =>
              e.reason === "Unhealthy" &&
              current.has(e.involvedObject.name) &&
              /Readiness probe failed.*statuscode: 404/.test(e.message ?? ""),
          );
        },
      },
      {
        label: "no payments-api pod is Ready",
        pass: (s) => !podsOf(s, "payments-api").some(isReady),
      },
      {
        label: "Service payments-api has no ready endpoints",
        pass: (s) => endpointAddresses(s, "payments-api", true).length === 0,
      },
    ],
  },
  "selector-mismatch": {
    logsFrom: ["storefront"],
    checks: [
      { label: "catalog pods are ready", pass: (s) => available(s, "catalog") },
      {
        label: "Service catalog has no endpoints",
        pass: (s) => endpointAddresses(s, "catalog", false).length === 0,
      },
      {
        label: "storefront logs failed requests",
        pass: (s) => (s.logs.storefront ?? "").includes("ERROR"),
      },
    ],
  },
  healthy: {
    logsFrom: ["inventory-sync"],
    checks: [
      {
        label: "all inventory deployments are available",
        pass: (s) => available(s, "inventory") && available(s, "inventory-sync"),
      },
      {
        label: "Service inventory has ready endpoints",
        pass: (s) => endpointAddresses(s, "inventory", true).length > 0,
      },
      {
        label: "inventory-sync reports successful syncs",
        pass: (s) => (s.logs["inventory-sync"] ?? "").includes("sync ok"),
      },
    ],
  },
};

export function checksFor(id: string): ScenarioChecks {
  const c = SCENARIO_CHECKS[id];
  if (!c) throw new Error(`no checks defined for scenario ${id}`);
  return c;
}

export function runChecks(id: string, snapshot: Snapshot): CheckResult[] {
  return checksFor(id).checks.map((c) => ({ label: c.label, pass: c.pass(snapshot) }));
}

export function stateOf(snapshot: Snapshot, results: CheckResult[]): ScenarioState {
  if (snapshot.deployments.length === 0) return "absent";
  return results.every((r) => r.pass) ? "reproduced" : "settling";
}
