import { existsSync } from "node:fs";
import fastifyStatic from "@fastify/static";
import Fastify, { type FastifyInstance } from "fastify";
import { type ClusterHealth, type PhoenixStatus, REQUEST_HEADER, REQUEST_HEADER_VALUE } from "../shared/types.ts";
import { describeClusterError } from "./kube.ts";
import { ScenarioBusyError, type ScenarioService, UnknownScenarioError } from "./service.ts";

interface Options {
  service: ScenarioService;
  phoenixUrl?: string; // the link the browser opens
  phoenixInternalUrl?: string; // where the API checks Phoenix; defaults to phoenixUrl
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

export function buildApp({ service, phoenixUrl, phoenixInternalUrl, webDir, logger = false }: Options): FastifyInstance {
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
    if (!phoenixUrl) return { url: null, reachable: false };
    const reachable = await fetch(`${phoenixInternalUrl ?? phoenixUrl}/healthz`, { signal: AbortSignal.timeout(1500) }).then(
      (r) => r.ok,
      () => false,
    );
    return { url: phoenixUrl, reachable };
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
