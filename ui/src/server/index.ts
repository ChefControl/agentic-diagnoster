import { buildApp } from "./app.ts";
import { config } from "./config.ts";
import { loadScenarios } from "./scenarios.ts";
import { ScenarioService } from "./service.ts";

const service = new ScenarioService(loadScenarios(config.scenariosDir), config.scenarioManagerKubeconfig);
const app = buildApp({
  service,
  phoenixUrl: config.phoenixUrl,
  phoenixInternalUrl: config.phoenixInternalUrl,
  webDir: config.production ? config.webDir : undefined,
  logger: true,
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    app.close().then(() => process.exit(0));
  });
}

app.listen({ port: config.port, host: config.host }).catch((err) => {
  app.log.error(err);
  process.exit(1);
});
