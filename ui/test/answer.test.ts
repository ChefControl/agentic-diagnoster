import { describe, expect, it } from "vitest";
import type { AgentRun } from "../src/shared/types.ts";
import { categoryLabel, citedCalls, describeCall, headline, modelName } from "../src/web/answer.ts";
import { MAX_CHATS, remember, type StoredChat } from "../src/web/chats.ts";

const run = (over: Partial<AgentRun["report"]> = {}): AgentRun => ({
  run_id: "r1",
  status: over.status ?? "complete",
  model: "openai:gpt-6-luna",
  cluster: "live",
  trace_url: "http://localhost:6006/redirects/traces/abc",
  report: {
    status: "complete",
    stop_reason: null,
    trace_id: "abc",
    tool_calls: [
      { id: "tc_1", tool: "list_pods", args: { namespace: "payments" }, sources: [] },
      { id: "tc_2", tool: "get_deployment", args: { namespace: "payments", name: "payments-api" }, sources: [] },
    ],
    diagnosis: {
      root_cause: { category: "bad_probe", resource: "deployment/payments-api", explanation: "The probe path is wrong." },
      evidence: [{ fact: "readiness probe GET /healthz", tool_call_ids: ["tc_2"] }],
      next_action: { summary: "Fix the path.", kind: "recommend", proposed_fix: null },
    },
    ...over,
  },
});

describe("the answer in plain words", () => {
  it("names the cause and where it is", () => {
    expect(headline(run())).toBe("Bad probe in deployment/payments-api");
  });

  it("says when nothing is wrong", () => {
    const healthy = run();
    healthy.report.diagnosis!.root_cause = { category: "none", resource: null, explanation: "All pods ready." };
    expect(headline(healthy)).toBe("Nothing looks wrong.");
  });

  it("says why a run stopped", () => {
    const stopped = run({ status: "stopped", stop_reason: "timeout", diagnosis: null });
    expect(headline({ ...stopped, status: "stopped" })).toBe("The agent stopped without an answer: it ran out of time.");
  });

  it("describes each cited read", () => {
    const r = run();
    expect(citedCalls(r, ["tc_2", "tc_9"]).map(describeCall)).toEqual(["read deployment payments-api in payments"]);
    expect(describeCall(r.report.tool_calls[0]!)).toBe("listed the pods in payments");
  });

  it("labels categories and models for people", () => {
    expect(categoryLabel("missing_config")).toBe("Missing config");
    expect(categoryLabel("rbac")).toBe("RBAC");
    expect(modelName("openai:gpt-6-luna")).toBe("gpt-6-luna");
  });
});

describe("chat history", () => {
  const chat = (id: string): StoredChat => ({ id, question: `q${id}`, askedAt: "2026-11-19T00:00:00Z" });

  it("keeps the newest first, once each, and at most the last 20", () => {
    let chats: StoredChat[] = [];
    for (let i = 0; i < 25; i++) chats = remember(chats, chat(String(i)));
    expect(chats).toHaveLength(MAX_CHATS);
    expect(chats[0]!.id).toBe("24");
    chats = remember(chats, { ...chat("10"), error: "x" });
    expect(chats[0]).toMatchObject({ id: "10", error: "x" });
    expect(chats.filter((c) => c.id === "10")).toHaveLength(1);
  });
});
