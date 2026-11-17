import { useCallback, useEffect, useState } from "react";
import type { ClusterHealth, PhoenixStatus, ScenarioSpec, ScenarioStatus, ScenarioSummary } from "../shared/types.ts";

async function call<T>(method: string, path: string): Promise<T> {
  const res = await fetch(path, { method });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `Request failed (${res.status})`);
  return body as T;
}

export const api = {
  health: () => call<ClusterHealth>("GET", "/api/health"),
  phoenix: () => call<PhoenixStatus>("GET", "/api/phoenix"),
  scenarios: () => call<ScenarioSummary[]>("GET", "/api/scenarios"),
  status: (id: string) => call<ScenarioStatus>("GET", `/api/scenarios/${id}`),
  spec: (id: string) => call<ScenarioSpec>("GET", `/api/scenarios/${id}/spec`),
  deploy: (id: string) => call<ScenarioStatus>("POST", `/api/scenarios/${id}/deploy`),
  reset: (id: string) => call<ScenarioStatus>("POST", `/api/scenarios/${id}/reset`),
  remove: (id: string) => call<ScenarioStatus>("DELETE", `/api/scenarios/${id}`),
};

// Fetch now and every `ms` while mounted; refetch whenever `key` changes.
export function usePoll<T>(fetcher: () => Promise<T>, ms: number, key: string) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setData(undefined);
    const run = () =>
      fetcher().then(
        (d) => alive && (setData(d), setError(undefined)),
        (e: Error) => alive && setError(e.message),
      );
    run();
    const timer = setInterval(run, ms);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ms, tick]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, refresh, setData };
}
