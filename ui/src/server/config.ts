import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// All configuration comes from the environment, with defaults that work when
// running from a checkout of this repository.
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

export const config = {
  scenariosDir: process.env.SCENARIOS_DIR ?? resolve(repoRoot, "scenarios"),
  // The scenario tooling's own identity: may change workloads in scenario namespaces, nothing else.
  scenarioManagerKubeconfig:
    process.env.SCENARIO_MANAGER_KUBECONFIG ?? resolve(repoRoot, ".kube/scenario-manager.kubeconfig"),
};
