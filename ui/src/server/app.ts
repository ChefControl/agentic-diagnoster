import { existsSync } from "node:fs";
import fastifyStatic from "@fastify/static";
import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";
import {
  type AgentQuestion,
  type ClusterHealth,
  type PhoenixStatus,
  REQUEST_HEADER,
  REQUEST_HEADER_VALUE,
} from "../shared/types.ts";
import { describeClusterError } from "./kube.ts";
import { ScenarioBusyError, type ScenarioService, UnknownScenarioError } from "./service.ts";

interface Options {
  service: ScenarioService;
  phoenixUrl?: string; // the link the browser opens
  phoenixInternalUrl?: string; // where the API checks Phoenix; defaults to phoenixUrl
  agentUrl?: string; // the agent service; the chat's questions go there
  webDir?: string; // serve the built UI from here (production)
  logger?: boolean;
}

type IdParams = { Params: { id: string } };

// The API runs on the reviewer's machine and can deploy and reset scenarios,
// so a web page from another site must not be able to use it.
//
// - Host check: a site can point its own domain at 127.0.0.1 (DNS
//   rebinding) and then read our responses. Its requests carry its own
//   domain in the Host header, so only local names are accepted.
// - Header on every change: browsers send a plain POST to any site without
//   asking first, but a request with a custom header needs the server's
//   permission (a CORS preflight), which this server never gives. So a
//   foreign page can't trigger deploy, reset or remove.
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

// "localhost:5173" -> "localhost", "[::1]:8787" -> "[::1]"
function hostname(host: string | undefined): string {
  if (!host) return "";
  const name = host.startsWith("[") ? host.slice(0, host.indexOf("]") + 1) : host.split(":")[0]!;
  return name.toLowerCase();
}

const CHANGES = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// Both created by the agent's phoenix-setup (agent/src/diagnoster/evals/setup.py).
export const EVAL_DATASET = "diagnoster-scenarios";
export const AGENT_PROMPT = "diagnoster-system";

// The id of a Phoenix dataset or prompt with this name, from a REST listing.
async function findId(phoenix: string, listing: string, name: string): Promise<string | null> {
  try {
    const res = await fetch(`${phoenix}${listing}`, { signal: AbortSignal.timeout(1500) });
    if (!res.ok) return null;
    const body = (await res.json()) as { data?: { id: string; name: string }[] };
    return body.data?.find((d) => d.name === name)?.id ?? null;
  } catch {
    return null;
  }
}

// The Playground with the agent's prompt (which also picks the agent as the model)
// and the eval dataset with its evaluators. Without the prompt, the Playground
// would open on its own "You are a chatbot" prompt, and the agent would run with that.
async function evalsUrl(phoenixUrl: string, internal: string): Promise<string | null> {
  const [datasetId, promptId] = await Promise.all([
    findId(internal, `/v1/datasets?name=${encodeURIComponent(EVAL_DATASET)}`, EVAL_DATASET),
    findId(internal, "/v1/prompts?limit=100", AGENT_PROMPT),
  ]);
  if (!datasetId || !promptId) return null;
  return `${phoenixUrl}/playground?promptId=${encodeURIComponent(promptId)}&datasetId=${encodeURIComponent(datasetId)}`;
}

// A run can take the agent's whole time limit (80 s in Compose); an approved
// fix adds up to 90 s of checking that it worked.
const AGENT_TIMEOUT_MS = 240_000;

export function buildApp({
  service,
  phoenixUrl,
  phoenixInternalUrl,
  agentUrl,
  webDir,
  logger = false,
}: Options): FastifyInstance {
  const app = Fastify({ logger });

  app.addHook("onRequest", async (req, reply) => {
    if (!LOCAL_HOSTS.has(hostname(req.headers.host))) {
      return reply.code(403).send({ error: "This server only answers requests addressed to localhost." });
    }
    if (CHANGES.has(req.method) && req.headers[REQUEST_HEADER] !== REQUEST_HEADER_VALUE) {
      return reply.code(403).send({ error: `Changes need the ${REQUEST_HEADER} header, which the UI sends.` });
    }
  });

  app.setErrorHandler((err, _req, reply) => {
    if (err instanceof UnknownScenarioError) return reply.code(404).send({ error: err.message });
    if (err instanceof ScenarioBusyError) return reply.code(409).send({ error: err.message });
    const clusterMessage = describeClusterError(err);
    if (clusterMessage) return reply.code(503).send({ error: clusterMessage });
    app.log.error(err);
    return reply.code(500).send({ error: err instanceof Error ? err.message : String(err) });
  });

  app.get("/api/health", async (): Promise<ClusterHealth> => {
    try {
      await service.ping();
      return { reachable: true };
    } catch (err) {
      return { reachable: false, message: describeClusterError(err) ?? String(err) };
    }
  });

  // Phoenix holds the traces and eval experiments; the UI links to it.
  app.get("/api/phoenix", async (): Promise<PhoenixStatus> => {
    if (!phoenixUrl) return { url: null, reachable: false, evalsUrl: null };
    const internal = phoenixInternalUrl ?? phoenixUrl;
    const reachable = await fetch(`${internal}/healthz`, { signal: AbortSignal.timeout(1500) }).then(
      (r) => r.ok,
      () => false,
    );
    return { url: phoenixUrl, reachable, evalsUrl: reachable ? await evalsUrl(phoenixUrl, internal) : null };
  });

  // The chat asks the agent through this server, so the browser only ever talks
  // to us (and the header check above covers the chat too).
  async function toAgent(reply: FastifyReply, path: string, body: unknown) {
    if (!agentUrl) return reply.code(503).send({ error: "No agent is configured (AGENT_URL)." });
    let res: Response;
    try {
      res = await fetch(`${agentUrl}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(AGENT_TIMEOUT_MS),
      });
    } catch (err) {
      const timedOut = err instanceof Error && err.name === "TimeoutError";
      return reply.code(timedOut ? 504 : 503).send({
        error: timedOut
          ? "The agent took too long to answer."
          : `The agent isn't reachable at ${agentUrl}. Start it with \`docker compose up -d agent\`.`,
      });
    }
    const answer = (await res.json().catch(() => ({}))) as { error?: { message?: string } };
    if (!res.ok) return reply.code(res.status).send({ error: answer.error?.message ?? `The agent failed (${res.status}).` });
    return answer;
  }

  app.post<{ Body: Partial<AgentQuestion> }>("/api/agent/runs", async (req, reply) => {
    const { question, session_id } = req.body ?? {};
    if (typeof question !== "string" || !question.trim()) return reply.code(400).send({ error: "Ask a question." });
    return toAgent(reply, "/v1/runs", { question, session_id });
  });

  // The person's answer to the fix a run waits on. The agent applies it, not this server.
  app.post<{ Params: { id: string }; Body: { approved?: unknown } }>("/api/agent/runs/:id/approval", async (req, reply) => {
    if (!/^[0-9a-f]{32}$/.test(req.params.id)) return reply.code(404).send({ error: "No such run." });
    const approved = req.body?.approved;
    if (typeof approved !== "boolean") return reply.code(400).send({ error: "Say approved: true or false." });
    return toAgent(reply, `/v1/runs/${req.params.id}/approval`, { approved });
  });

  app.get("/api/scenarios", () => service.summaries());
  app.get<IdParams>("/api/scenarios/:id", (req) => service.status(req.params.id));
  app.get<IdParams>("/api/scenarios/:id/spec", (req) => service.get(req.params.id).spec);

  app.post<IdParams>("/api/scenarios/:id/deploy", async (req) => {
    await service.deploy(req.params.id);
    return service.status(req.params.id);
  });
  app.post<IdParams>("/api/scenarios/:id/reset", async (req) => {
    await service.reset(req.params.id);
    return service.status(req.params.id);
  });
  app.delete<IdParams>("/api/scenarios/:id", async (req) => {
    await service.remove(req.params.id);
    return service.status(req.params.id);
  });

  if (webDir && existsSync(webDir)) {
    app.register(fastifyStatic, { root: webDir });
    app.setNotFoundHandler((req, reply) =>
      req.url.startsWith("/api/") ? reply.code(404).send({ error: "not found" }) : reply.sendFile("index.html"),
    );
  }

  return app;
}
