// The agent's answer in plain words. The agent replies JSON (its report);
// these turn that into what the chat says. Pure functions, tested on their own.
import type { AgentRun, AgentStopReason, AgentToolCall } from "../shared/types.ts";

// "missing_config" -> "Missing config"
export function categoryLabel(category: string): string {
  const words = category.replaceAll("_", " ");
  return words === "rbac" ? "RBAC" : words.charAt(0).toUpperCase() + words.slice(1);
}

const STOPPED: Record<AgentStopReason, string> = {
  request_limit: "it used up its model calls before reaching an answer",
  tool_call_limit: "it used up its cluster reads before reaching an answer",
  timeout: "it ran out of time",
  invalid_output: "its answer didn't have the required shape, even after retries",
  model_error: "the model failed",
};

export function headline(run: AgentRun): string {
  const diagnosis = run.report.diagnosis;
  if (run.status === "stopped" || !diagnosis) {
    const reason = run.report.stop_reason ? STOPPED[run.report.stop_reason] : "it didn't finish";
    return `The agent stopped without an answer: ${reason}.`;
  }
  if (diagnosis.root_cause.category === "none") return "Nothing looks wrong.";
  return `${categoryLabel(diagnosis.root_cause.category)} in ${diagnosis.root_cause.resource}`;
}

const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);

// What one cluster read was, e.g. "read the logs of payments-api in payments".
export function describeCall(call: AgentToolCall): string {
  const ns = str(call.args.namespace);
  const where = ns ? ` in ${ns}` : "";
  const name = str(call.args.name);
  const workload = str(call.args.workload);
  switch (call.tool) {
    case "list_workloads":
      return `listed the workloads${where}`;
    case "list_pods":
      return `listed the pods${where}`;
    case "get_pod_logs":
      return `read the logs of ${workload ?? "a workload"}${where}`;
    case "get_events":
      return workload ? `read the events of ${workload}${where}` : `read the events${where}`;
    case "get_service":
      return `read service ${name ?? ""}${where}`;
    case "get_deployment":
      return `read deployment ${name ?? ""}${where}`;
    case "get_configmap":
      return `read ConfigMap ${name ?? ""}${where}`;
    default:
      return `${call.tool}${where}`;
  }
}

// The reads a fact cites, in the order they happened. Unknown ids are dropped
// (the agent checks citations, so there shouldn't be any).
export function citedCalls(run: AgentRun, ids: string[]): AgentToolCall[] {
  const wanted = new Set(ids);
  return run.report.tool_calls.filter((c) => wanted.has(c.id));
}

// "openai:gpt-6-luna" -> "gpt-6-luna"
export function modelName(model: string): string {
  return model.includes(":") ? model.slice(model.indexOf(":") + 1) : model;
}
