import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { buildApp } from "../src/server/app.ts";
import { loadScenarios } from "../src/server/scenarios.ts";
import { ScenarioService } from "../src/server/service.ts";

// No cluster here: the kubeconfig path does not exist, which is exactly the
// state of a fresh checkout before ./cluster.sh --up.
const service = new ScenarioService(loadScenarios(resolve(import.meta.dirname, "../../scenarios")), "/nonexistent/kubeconfig");
// Nothing listens on port 9, so Phoenix counts as not running.
const app = buildApp({ service, phoenixUrl: "http://127.0.0.1:9" });

describe("API without a cluster", () => {
  it("reports the cluster as unreachable with a fix", async () => {
    const res = await app.inject({ method: "GET", url: "/api/health" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ reachable: false });
    expect(res.json().message).toContain("./cluster.sh --up");
  });

  it("answers 503 with the same guidance on scenario calls", async () => {
    const res = await app.inject({ method: "GET", url: "/api/scenarios/bad-probe" });
    expect(res.statusCode).toBe(503);
    expect(res.json().error).toContain("./cluster.sh --up");
  });

  it("answers 404 for an unknown scenario", async () => {
    const res = await app.inject({ method: "POST", url: "/api/scenarios/nope/deploy" });
    expect(res.statusCode).toBe(404);
  });

  it("reports Phoenix as not running, with the URL to open", async () => {
    const res = await app.inject({ method: "GET", url: "/api/phoenix" });
    expect(res.json()).toEqual({ url: "http://127.0.0.1:9", reachable: false });
  });

  it("serves the scenario spec without touching the cluster", async () => {
    const res = await app.inject({ method: "GET", url: "/api/scenarios/bad-probe/spec" });
    expect(res.statusCode).toBe(200);
    expect(res.json().category).toBe("bad_probe");
  });
});
