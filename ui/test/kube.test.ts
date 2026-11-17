import type { CoreV1Event } from "@kubernetes/client-node";
import { describe, expect, it } from "vitest";
import { summarizeEvents } from "../src/server/kube.ts";

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
