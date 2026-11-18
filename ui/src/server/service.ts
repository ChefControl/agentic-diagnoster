import type { ScenarioStatus, ScenarioSummary } from "../shared/types.ts";
import { checksFor, runChecks, stateOf } from "./checks.ts";
import { ScenarioCluster, summarizeEvents, summarizePods } from "./kube.ts";
import type { Scenario } from "./scenarios.ts";

// Scenario operations shared by the HTTP API and the CI command line.
export class ScenarioService {
  private readonly busy = new Set<string>();

  constructor(
    private readonly scenarios: Scenario[],
    private readonly kubeconfigPath: string,
  ) {}

  private cluster(): ScenarioCluster {
    return new ScenarioCluster(this.kubeconfigPath);
  }

  get(id: string): Scenario {
    const s = this.scenarios.find((s) => s.id === id);
    if (!s) throw new UnknownScenarioError(id);
    return s;
  }

  ids(): string[] {
    return this.scenarios.map((s) => s.id);
  }

  async ping(): Promise<void> {
    await this.cluster().ping(this.scenarios[0]!.namespace);
  }

  async status(id: string): Promise<ScenarioStatus> {
    const scenario = this.get(id);
    const cluster = this.cluster();
    const snapshot = await cluster.snapshot(scenario.namespace, checksFor(id).logsFrom);
    const checks = runChecks(id, snapshot);
    const changes = snapshot.deployments.length > 0 ? await cluster.changes(scenario) : [];
    return {
      id,
      namespace: scenario.namespace,
      state: stateOf(snapshot, checks, changes),
      checks,
      changes,
      pods: summarizePods(snapshot),
      events: summarizeEvents(snapshot.events),
    };
  }

  async summaries(): Promise<ScenarioSummary[]> {
    return Promise.all(
      this.scenarios.map(async (s) => ({
        id: s.id,
        namespace: s.namespace,
        title: s.title,
        question: s.question,
        fault: s.spec.fault,
        category: s.spec.category,
        remediable: s.remediable,
        state: (await this.status(s.id)).state,
      })),
    );
  }

  deploy(id: string): Promise<void> {
    return this.exclusive(id, () => this.cluster().deploy(this.get(id)));
  }

  remove(id: string): Promise<void> {
    return this.exclusive(id, () => this.cluster().remove(this.get(id).namespace));
  }

  // Back to the designed broken state: fresh pods, fresh logs, no leftover fixes.
  reset(id: string): Promise<void> {
    return this.exclusive(id, async () => {
      const cluster = this.cluster();
      const scenario = this.get(id);
      await cluster.remove(scenario.namespace);
      await cluster.deploy(scenario);
    });
  }

  private async exclusive(id: string, op: () => Promise<void>): Promise<void> {
    this.get(id); // unknown scenario is a 404, before touching the cluster
    if (this.busy.has(id)) throw new ScenarioBusyError(id);
    this.busy.add(id);
    try {
      await op();
    } finally {
      this.busy.delete(id);
    }
  }
}

export class UnknownScenarioError extends Error {
  constructor(id: string) {
    super(`unknown scenario: ${id}`);
  }
}

export class ScenarioBusyError extends Error {
  constructor(id: string) {
    super(`scenario ${id} is already being changed, try again in a moment`);
  }
}
