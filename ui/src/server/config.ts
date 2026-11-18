import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// All configuration comes from the environment, with defaults that work when
// running from a checkout of this repository.
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

export const config = {
  port: Number(process.env.API_PORT ?? 8787),
  host: process.env.HOST ?? "127.0.0.1",
  scenariosDir: process.env.SCENARIOS_DIR ?? resolve(repoRoot, "scenarios"),
  // The UI's own identity: may change workloads in scenario namespaces, nothing else.
  scenarioManagerKubeconfig:
    process.env.SCENARIO_MANAGER_KUBECONFIG ?? resolve(repoRoot, ".kube/scenario-manager.kubeconfig"),
  // Where traces and eval experiments live. The browser opens this URL.
  phoenixUrl: (process.env.PHOENIX_URL ?? "http://localhost:6006").replace(/\/$/, ""),
  // Where the API checks that Phoenix is up. Usually the same URL, but inside a
  // container "localhost" is the container itself, so Compose sets
  // http://phoenix:6006 here while the browser keeps http://localhost:6006.
  phoenixInternalUrl: (process.env.PHOENIX_INTERNAL_URL ?? process.env.PHOENIX_URL ?? "http://localhost:6006").replace(
    /\/$/,
    "",
  ),
  webDir: process.env.WEB_DIR ?? resolve(repoRoot, "ui/dist/web"),
  production: process.env.NODE_ENV === "production",
};
