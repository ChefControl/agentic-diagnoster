import { existsSync } from "node:fs";
import {
  ApiException,
  AppsV1Api,
  CoreV1Api,
  DiscoveryV1Api,
  KubeConfig,
  KubernetesObjectApi,
  PatchStrategy,
  type CoreV1Event,
} from "@kubernetes/client-node";
import type { EventSummary, PodSummary } from "../shared/types.ts";
import type { Snapshot } from "./checks.ts";
import type { Scenario } from "./scenarios.ts";

const FIELD_MANAGER = "agentic-diagnoster-ui";
const LOG_TAIL_LINES = 100;

export class ClusterUnavailableError extends Error {}

// Talks to the cluster as the scenario-manager identity. The kubeconfig is
// read on every call, so a recreated cluster (new token) needs no restart.
export class ScenarioCluster {
  private readonly core: CoreV1Api;
  private readonly apps: AppsV1Api;
  private readonly discovery: DiscoveryV1Api;
  private readonly objects: KubernetesObjectApi;

  constructor(kubeconfigPath: string) {
    if (!existsSync(kubeconfigPath)) {
      throw new ClusterUnavailableError(
        `No kubeconfig at ${kubeconfigPath}. Run ./cluster.sh --up to create the cluster.`,
      );
    }
    const kc = new KubeConfig();
    kc.loadFromFile(kubeconfigPath);
    this.core = kc.makeApiClient(CoreV1Api);
    this.apps = kc.makeApiClient(AppsV1Api);
    this.discovery = kc.makeApiClient(DiscoveryV1Api);
    this.objects = KubernetesObjectApi.makeApiClient(kc);
  }

  async ping(namespace: string): Promise<void> {
    await this.core.listNamespacedPod({ namespace, limit: 1 });
  }

  // Server-side apply: creates missing objects and restores changed fields.
  async deploy(scenario: Scenario): Promise<void> {
    for (const obj of scenario.objects) {
      await this.objects.patch(obj, undefined, undefined, FIELD_MANAGER, true, PatchStrategy.ServerSideApply);
    }
  }

  // Delete the scenario's workloads but keep the namespace: its access rules
  // belong to cluster.sh, and this identity could not recreate them.
  async remove(namespace: string): Promise<void> {
    const [deployments, services, configMaps] = await Promise.all([
      this.apps.listNamespacedDeployment({ namespace }),
      this.core.listNamespacedService({ namespace }),
      this.core.listNamespacedConfigMap({ namespace }),
    ]);
    await Promise.all([
      ...deployments.items.map((d) =>
        this.apps.deleteNamespacedDeployment({ name: d.metadata!.name!, namespace, propagationPolicy: "Background" }),
      ),
      ...services.items.map((s) => this.core.deleteNamespacedService({ name: s.metadata!.name!, namespace })),
      ...configMaps.items
        .filter((c) => c.metadata?.name !== "kube-root-ca.crt") // managed by Kubernetes
        .map((c) => this.core.deleteNamespacedConfigMap({ name: c.metadata!.name!, namespace })),
    ]);
    // Some scenario containers ignore SIGTERM; don't wait out their grace period.
    await this.core.deleteCollectionNamespacedPod({ namespace, gracePeriodSeconds: 0 });
    await this.waitForNoPods(namespace, 60_000);
    // Events outlive their pods by an hour. Clear them so a reset scenario
    // shows only its own history, to the checks and to the agent.
    await this.core.deleteCollectionNamespacedEvent({ namespace });
  }

  async snapshot(namespace: string, logsFrom: string[]): Promise<Snapshot> {
    const [deployments, pods, events, slices] = await Promise.all([
      this.apps.listNamespacedDeployment({ namespace }),
      this.core.listNamespacedPod({ namespace }),
      this.core.listNamespacedEvent({ namespace }),
      this.discovery.listNamespacedEndpointSlice({ namespace }),
    ]);
    const logs: Record<string, string> = {};
    for (const app of logsFrom) {
      const pod = pods.items.find((p) => p.metadata?.labels?.app === app && !p.metadata.deletionTimestamp);
      if (pod) logs[app] = await this.podLogs(namespace, pod.metadata!.name!);
    }
    return {
      deployments: deployments.items,
      pods: pods.items.filter((p) => !p.metadata?.deletionTimestamp),
      events: events.items,
      endpointSlices: slices.items,
      logs,
    };
  }

  // In a crash loop the useful line can be in the current (just crashed)
  // container or in the previous one, depending on timing. Read both.
  private async podLogs(namespace: string, name: string): Promise<string> {
    const read = (previous: boolean) =>
      this.core
        .readNamespacedPodLog({ name, namespace, previous, tailLines: LOG_TAIL_LINES })
        .catch(() => "");
    const [previous, current] = await Promise.all([read(true), read(false)]);
    return `${previous}${current}`;
  }

  private async waitForNoPods(namespace: string, timeoutMs: number): Promise<void> {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      const pods = await this.core.listNamespacedPod({ namespace });
      if (pods.items.length === 0) return;
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error(`pods in ${namespace} did not terminate within ${timeoutMs / 1000}s`);
  }
}

export function summarizePods(snapshot: Snapshot): PodSummary[] {
  return snapshot.pods.map((p) => {
    const statuses = p.status?.containerStatuses ?? [];
    const ready = statuses.filter((c) => c.ready).length;
    const waiting = statuses.find((c) => c.state?.waiting)?.state?.waiting?.reason;
    const terminated = statuses.find((c) => c.state?.terminated)?.state?.terminated?.reason;
    return {
      name: p.metadata?.name ?? "",
      ready: `${ready}/${statuses.length || p.spec?.containers.length || 0}`,
      phase: p.status?.phase ?? "Unknown",
      reason: waiting ?? terminated ?? null,
      restarts: statuses.reduce((n, c) => n + c.restartCount, 0),
    };
  });
}

export function summarizeEvents(events: CoreV1Event[], limit = 12): EventSummary[] {
  const warning = (e: CoreV1Event) => (e.type === "Warning" ? 1 : 0);
  const when = (e: CoreV1Event) => new Date(e.lastTimestamp ?? e.eventTime ?? e.metadata.creationTimestamp ?? 0);
  return [...events]
    // Warnings first: they are what explains a fault; Normal events are mostly
    // scheduling and image pulls.
    .sort((a, b) => warning(b) - warning(a) || when(b).getTime() - when(a).getTime())
    .slice(0, limit)
    .map((e) => ({
      type: e.type ?? "Normal",
      reason: e.reason ?? "",
      object: `${e.involvedObject.kind?.toLowerCase()}/${e.involvedObject.name}`,
      message: e.message ?? "",
      count: e.count ?? 1,
      lastSeen: when(e).toISOString(),
    }));
}

// Turn low-level connection failures into one actionable message.
export function describeClusterError(err: unknown): string | null {
  if (err instanceof ClusterUnavailableError) return err.message;
  const code = (err as { code?: string; cause?: { code?: string } })?.cause?.code ?? (err as { code?: string })?.code;
  if (code === "ECONNREFUSED" || code === "ENOTFOUND" || code === "ECONNRESET") {
    return "Can't reach the cluster. Run ./cluster.sh --up to create it.";
  }
  if (err instanceof ApiException && err.code === 401) {
    return "The cluster rejected the UI's token (expired or cluster recreated). Run ./cluster.sh --up to issue a new one.";
  }
  return null;
}
