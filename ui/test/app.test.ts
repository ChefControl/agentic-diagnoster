import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { AGENT_PROMPT, buildApp, EVAL_DATASET } from "../src/server/app.ts";
import { loadScenarios } from "../src/server/scenarios.ts";
import { ScenarioService } from "../src/server/service.ts";
import { REQUEST_HEADER, REQUEST_HEADER_VALUE } from "../src/shared/types.ts";

// No cluster here: the kubeconfig path does not exist, which is exactly the
// state of a fresh checkout before ./cluster.sh --up.
const service = new ScenarioService(loadScenarios(resolve(import.meta.dirname, "../../scenarios")), "/nonexistent/kubeconfig");
// Nothing listens on port 9, so Phoenix counts as not running.
const app = buildApp({ service, phoenixUrl: "http://127.0.0.1:9" });
const fromUi = { [REQUEST_HEADER]: REQUEST_HEADER_VALUE };

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
    const res = await app.inject({ method: "POST", url: "/api/scenarios/nope/deploy", headers: fromUi });
    expect(res.statusCode).toBe(404);
  });

  it("reports Phoenix as not running, with the URL to open", async () => {
    const res = await app.inject({ method: "GET", url: "/api/phoenix" });
    expect(res.json()).toEqual({ url: "http://127.0.0.1:9", reachable: false, evalsUrl: null });
  });

  it("serves the scenario spec without touching the cluster", async () => {
    const res = await app.inject({ method: "GET", url: "/api/scenarios/bad-probe/spec" });
    expect(res.statusCode).toBe(200);
    expect(res.json().category).toBe("bad_probe");
  });
});

// Requests another website could make from the reviewer's browser.
describe("API refuses other sites", () => {
  it("refuses a change without the UI's header, before touching the cluster", async () => {
    for (const [method, url] of [
      ["POST", "/api/scenarios/bad-probe/deploy"],
      ["POST", "/api/scenarios/bad-probe/reset"],
      ["DELETE", "/api/scenarios/bad-probe"],
    ] as const) {
      const res = await app.inject({ method, url, headers: { origin: "https://some-other-site.example" } });
      expect(res.statusCode, `${method} ${url}`).toBe(403);
      expect(res.json().error).toContain(REQUEST_HEADER);
    }
  });

  it("refuses a change whose header has the wrong value", async () => {
    const res = await app.inject({ method: "POST", url: "/api/scenarios/bad-probe/reset", headers: { [REQUEST_HEADER]: "1" } });
    expect(res.statusCode).toBe(403);
  });

  it("refuses requests addressed to another host name, so answer keys can't be read by DNS rebinding", async () => {
    const res = await app.inject({ method: "GET", url: "/api/scenarios/bad-probe/spec", headers: { host: "attacker.example" } });
    expect(res.statusCode).toBe(403);
    expect(res.body).not.toContain("bad_probe");
  });

  it.each(["localhost:5173", "127.0.0.1:8787", "[::1]:8787", "LOCALHOST"])("answers requests addressed to %s", async (host) => {
    const res = await app.inject({ method: "GET", url: "/api/scenarios/bad-probe/spec", headers: { host } });
    expect(res.statusCode).toBe(200);
  });

  it("does not need the header to read", async () => {
    const res = await app.inject({ method: "GET", url: "/api/scenarios/bad-probe/spec" });
    expect(res.statusCode).toBe(200);
  });
});

describe("Phoenix behind two URLs", () => {
  it("checks the internal URL but returns the public one for the browser", async () => {
    // A stand-in Phoenix that only the internal URL reaches, with no eval dataset yet.
    const phoenix = createServer((req, res) => res.writeHead(req.url === "/healthz" ? 200 : 404).end());
    await new Promise<void>((done) => phoenix.listen(0, "127.0.0.1", done));
    const internal = `http://127.0.0.1:${(phoenix.address() as AddressInfo).port}`;
    try {
      // The public URL points nowhere, so "reachable" can only come from the internal one.
      const twoUrls = buildApp({ service, phoenixUrl: "http://127.0.0.1:9", phoenixInternalUrl: internal });
      const res = await twoUrls.inject({ method: "GET", url: "/api/phoenix" });
      expect(res.json()).toEqual({ url: "http://127.0.0.1:9", reachable: true, evalsUrl: null });
    } finally {
      phoenix.close();
    }
  });
});

describe("the evals link", () => {
  // A stand-in Phoenix that lists the given datasets and prompts.
  async function withPhoenix(listed: { datasets: object[]; prompts: object[] }, check: (internal: string) => Promise<void>) {
    const phoenix = createServer((req, res) => {
      if (req.url === "/healthz") return res.writeHead(200).end();
      const data = req.url?.startsWith("/v1/datasets") ? listed.datasets : req.url?.startsWith("/v1/prompts") ? listed.prompts : null;
      if (!data) return res.writeHead(404).end();
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ data }));
    });
    await new Promise<void>((done) => phoenix.listen(0, "127.0.0.1", done));
    try {
      await check(`http://127.0.0.1:${(phoenix.address() as AddressInfo).port}`);
    } finally {
      phoenix.close();
    }
  }

  it("opens the Playground with the agent's prompt and the eval dataset, looked up by name", async () => {
    const datasets = [{ id: "RGF0YXNldDo5", name: "something-else" }, { id: "RGF0YXNldDox", name: EVAL_DATASET }];
    const prompts = [{ id: "UHJvbXB0OjI=", name: "action-evaluator-1" }, { id: "UHJvbXB0OjE=", name: AGENT_PROMPT }];
    await withPhoenix({ datasets, prompts }, async (internal) => {
      const res = await buildApp({ service, phoenixUrl: "http://localhost:6006", phoenixInternalUrl: internal }).inject({ method: "GET", url: "/api/phoenix" });
      expect(res.json().evalsUrl).toBe("http://localhost:6006/playground?promptId=UHJvbXB0OjE%3D&datasetId=RGF0YXNldDox");
    });
  });

  it("gives no link until both exist", async () => {
    await withPhoenix({ datasets: [{ id: "RGF0YXNldDox", name: EVAL_DATASET }], prompts: [] }, async (internal) => {
      const res = await buildApp({ service, phoenixUrl: "http://localhost:6006", phoenixInternalUrl: internal }).inject({ method: "GET", url: "/api/phoenix" });
      expect(res.json()).toEqual({ url: "http://localhost:6006", reachable: true, evalsUrl: null });
    });
  });
});
