import type { CoreV1Event, V1Deployment, V1EndpointSlice, V1Pod } from "@kubernetes/client-node";
import { describe, expect, it } from "vitest";
import { runChecks, type Snapshot, stateOf } from "../src/server/checks.ts";

// Minimal Kubernetes objects: only the fields the checks read.
const deployment = (name: string, replicas: number, available: number) =>
  ({ metadata: { name }, spec: { replicas }, status: { availableReplicas: available } }) as V1Deployment;

const pod = (name: string, app: string, opts: { ready?: boolean; restarts?: number; phase?: string } = {}) =>
  ({
    metadata: { name, labels: { app } },
    status: {
      phase: opts.phase ?? "Running",
      conditions: [{ type: "Ready", status: opts.ready ? "True" : "False" }],
      containerStatuses: [{ restartCount: opts.restarts ?? 0, ready: !!opts.ready }],
    },
  }) as unknown as V1Pod;

const event = (podName: string, message: string) =>
  ({ reason: "Unhealthy", message, involvedObject: { kind: "Pod", name: podName }, metadata: {} }) as CoreV1Event;

const slice = (service: string, ready: boolean[]) =>
  ({
    metadata: { labels: { "kubernetes.io/service-name": service } },
    endpoints: ready.map((r, i) => ({ addresses: [`10.0.0.${i}`], conditions: { ready: r } })),
  }) as unknown as V1EndpointSlice;

const snapshot = (s: Partial<Snapshot>): Snapshot => ({
  deployments: [],
  pods: [],
  events: [],
  endpointSlices: [],
  logs: {},
  ...s,
});

const verdict = (id: string, s: Snapshot) => stateOf(s, runChecks(id, s));

describe("scenario state", () => {
  it("is absent when nothing is deployed", () => {
    expect(verdict("missing-config", snapshot({}))).toBe("absent");
  });

  it("missing-config reproduces once checkout crashes and logs the missing variable", () => {
    const base = {
      deployments: [deployment("frontend", 1, 1), deployment("checkout", 1, 0)],
      pods: [pod("frontend-1", "frontend", { ready: true }), pod("checkout-1", "checkout", { restarts: 2 })],
    };
    expect(verdict("missing-config", snapshot(base))).toBe("settling");
    expect(
      verdict("missing-config", snapshot({ ...base, logs: { checkout: "FATAL: DATABASE_URL is not set" } })),
    ).toBe("reproduced");
  });

  describe("bad-probe", () => {
    const pods = [pod("payments-api-a", "payments-api"), pod("payments-api-b", "payments-api")];
    const broken = snapshot({
      deployments: [deployment("payments-api", 2, 0)],
      pods,
      events: [event("payments-api-a", "Readiness probe failed: HTTP probe failed with statuscode: 404")],
      endpointSlices: [slice("payments-api", [false, false])],
    });

    it("reproduces on a 404 probe failure with no ready endpoints", () => {
      expect(verdict("bad-probe", broken)).toBe("reproduced");
    });

    it("ignores a connection-refused startup blip", () => {
      const blip = { ...broken, events: [event("payments-api-a", 'Readiness probe failed: dial tcp: connection refused')] };
      expect(verdict("bad-probe", blip)).toBe("settling");
    });

    it("ignores probe events from pods of a previous run", () => {
      const stale = { ...broken, events: [event("payments-api-old", "Readiness probe failed: HTTP probe failed with statuscode: 404")] };
      expect(verdict("bad-probe", stale)).toBe("settling");
    });

    it("is no longer reproduced after the fix makes pods Ready", () => {
      const fixed = {
        ...broken,
        pods: [pod("payments-api-c", "payments-api", { ready: true }), pod("payments-api-d", "payments-api", { ready: true })],
        endpointSlices: [slice("payments-api", [true, true])],
      };
      expect(verdict("bad-probe", fixed)).toBe("settling");
    });
  });

  it("selector-mismatch needs healthy pods, an empty Service and client errors", () => {
    const s = snapshot({
      deployments: [deployment("catalog", 2, 2), deployment("storefront", 1, 1)],
      endpointSlices: [slice("catalog", [])],
      logs: { storefront: "ERROR: GET http://catalog/ failed" },
    });
    expect(verdict("selector-mismatch", s)).toBe("reproduced");
    expect(verdict("selector-mismatch", { ...s, endpointSlices: [slice("catalog", [true])] })).toBe("settling");
  });

  describe("healthy", () => {
    const s = snapshot({
      deployments: [deployment("inventory", 2, 2), deployment("inventory-sync", 1, 1)],
      pods: [
        pod("inventory-a", "inventory", { ready: true }),
        pod("inventory-b", "inventory", { ready: true }),
        pod("inventory-sync-a", "inventory-sync", { ready: true }),
      ],
      endpointSlices: [slice("inventory", [true, true])],
      logs: { "inventory-sync": "sync ok" },
    });

    it("reproduces only when everything works", () => {
      expect(verdict("healthy", s)).toBe("reproduced");
      expect(verdict("healthy", { ...s, logs: { "inventory-sync": "ERROR: sync failed" } })).toBe("settling");
    });

    it("is not reproduced once a container has restarted, since the answer key grades no restarts", () => {
      const restarted = { ...s, pods: [...s.pods.slice(0, 2), pod("inventory-sync-a", "inventory-sync", { ready: true, restarts: 1 })] };
      expect(verdict("healthy", restarted)).toBe("settling");
    });
  });

  describe("changed after deploy", () => {
    const change = { resource: "deployment/payments-api", field: ".spec.template.spec.containers[name=\"api\"].readinessProbe.httpGet.path", by: "kubectl-patch" };
    const reproduced = snapshot({
      deployments: [deployment("catalog", 2, 2), deployment("storefront", 1, 1)],
      endpointSlices: [slice("catalog", [])],
      logs: { storefront: "ERROR: GET http://catalog/ failed" },
    });

    it("wins over passing checks: the scenario is no longer the designed one", () => {
      expect(stateOf(reproduced, runChecks("selector-mismatch", reproduced), [change])).toBe("changed");
    });

    it("wins over failing checks, so a fixed scenario never looks like one still starting", () => {
      const fixed = { ...reproduced, endpointSlices: [slice("catalog", [true, true])] };
      expect(stateOf(fixed, runChecks("selector-mismatch", fixed), [change])).toBe("changed");
    });

    it("does not apply when nothing is deployed", () => {
      expect(stateOf(snapshot({}), [], [change])).toBe("absent");
    });
  });
});
