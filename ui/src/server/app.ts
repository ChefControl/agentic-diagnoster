import { existsSync } from "node:fs";
import fastifyStatic from "@fastify/static";
import Fastify, { type FastifyInstance } from "fastify";
import type { ClusterHealth, PhoenixStatus } from "../shared/types.ts";
import { describeClusterError } from "./kube.ts";
import { ScenarioBusyError, type ScenarioService, UnknownScenarioError } from "./service.ts";

interface Options {
  service: ScenarioService;
  phoenixUrl?: string;
  webDir?: string; // serve the built UI from here (production)
  logger?: boolean;
}

type IdParams = { Params: { id: string } };

export function buildApp({ service, phoenixUrl, webDir, logger = false }: Options): FastifyInstance {
  const app = Fastify({ logger });

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
    const reachable = await fetch(`${phoenixUrl}/healthz`, { signal: AbortSignal.timeout(1500) }).then(
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
