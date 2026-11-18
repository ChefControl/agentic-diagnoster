import type { CoreV1Event } from "@kubernetes/client-node";
import { describe, expect, it } from "vitest";
import { fieldConflicts, summarizeEvents } from "../src/server/kube.ts";

const event = (reason: string, type: string, at: string) =>
  ({
    reason,
    type,
    message: reason,
    lastTimestamp: new Date(at),
    involvedObject: { kind: "Pod", name: "p" },
    metadata: {},
  }) as unknown as CoreV1Event;

describe("event summary", () => {
  it("lists warnings first, then the newest events", () => {
    const events = [
      event("Pulled", "Normal", "2026-01-01T00:03:00Z"),
      event("Unhealthy", "Warning", "2026-01-01T00:01:00Z"),
      event("Started", "Normal", "2026-01-01T00:04:00Z"),
      event("BackOff", "Warning", "2026-01-01T00:02:00Z"),
    ];
    expect(summarizeEvents(events).map((e) => e.reason)).toEqual(["BackOff", "Unhealthy", "Started", "Pulled"]);
  });
});

describe("changes since deploy", () => {
  // The Status body the API server returns when a dry-run apply conflicts.
  const conflict = {
    kind: "Status",
    code: 409,
    reason: "Conflict",
    details: {
      causes: [
        {
          reason: "FieldManagerConflict",
          message: 'conflict with "kubectl-patch" using apps/v1',
          field: '.spec.template.spec.containers[name="api"].readinessProbe.httpGet.path',
        },
      ],
    },
  };

  it("names the changed field and who changed it", () => {
    expect(fieldConflicts("deployment/payments-api", conflict)).toEqual([
      {
        resource: "deployment/payments-api",
        field: '.spec.template.spec.containers[name="api"].readinessProbe.httpGet.path',
        by: "kubectl-patch",
      },
    ]);
  });

  it("reads the body whether the client returns it as text or as an object", () => {
    expect(fieldConflicts("deployment/payments-api", JSON.stringify(conflict))).toEqual(
      fieldConflicts("deployment/payments-api", conflict),
    );
  });

  it("still reports a change when the conflict has no details", () => {
    expect(fieldConflicts("service/catalog", "not json")).toEqual([
      { resource: "service/catalog", field: "(unknown field)", by: "unknown" },
    ]);
  });
});
