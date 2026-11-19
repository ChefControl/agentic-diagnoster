"""The agent's seven read-only tools.

Poka-yoke: each tool is hard to call wrongly.
- `namespace` is an enum of the namespaces the agent may look at.
- An unknown name returns a message listing the names that exist, so the model
  can correct itself; no exception reaches the model.
- Output is short, plain text with the facts a diagnosis needs, trimmed.
- Values under a credential-like name (password, token, API key...) are
  masked: tool output goes to the model's provider and into the traces.
- A rejected token (401) stops the run instead of reaching the model, which
  would otherwise diagnose its own expired credentials as a cluster fault.

Every call is recorded by our code: an id (tc_N) the model cites as evidence,
and the sources (which objects, which aspect) the call looked at. The output
starts with its id, so the model sees exactly what to cite.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from kubernetes.client.exceptions import ApiException
from pydantic_ai import FunctionToolset, RunContext

from .cluster import Cluster
from .contract import Diagnosis, Remediation, Source, ToolCall

MAX_LOG_PODS = 3
LOG_LINES = 50
MAX_LINE_CHARS = 300
MAX_EVENTS = 20
MAX_VALUE_CHARS = 2000
# Env var and ConfigMap key names whose values are masked.
SENSITIVE_NAME = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|credential", re.IGNORECASE)


@dataclass
class Recorder:
    """The run's tool calls, in order. Our record, not the model's account."""

    calls: list[ToolCall] = field(default_factory=list)

    def record(self, tool: str, args: dict[str, Any], sources: list[Source], provider_id: str | None) -> str:
        call_id = f"tc_{len(self.calls) + 1}"
        self.calls.append(ToolCall(id=call_id, provider_id=provider_id, tool=tool, args=args, sources=sources))
        return call_id

    def ids(self) -> set[str]:
        return {c.id for c in self.calls}


@dataclass
class RunDeps:
    cluster: Cluster
    recorder: Recorder = field(default_factory=Recorder)
    # Applies an approved fix (remediation.fix with real accounts). None where
    # nothing may change: the recorded cluster, evals.
    fixer: Callable[[Diagnosis], Remediation] | None = None
    # What became of the fix; our code writes it, the report carries it.
    remediation: Remediation | None = None


class NotFound(Exception):
    """A name the model asked for doesn't exist; the message lists what does."""


class ClusterAuthError(Exception):
    """The API server rejected the agent's token. Stops the run: no tool can work."""


AUTH_HINT = "The cluster rejected the agent's token (401 Unauthorized). Tokens last 24 h: run ./cluster.sh --up to issue fresh ones."


def _masked(name: str, value: str) -> str | None:
    """The value to show in place of a sensitive one, or None to show it as it is."""
    return f"(masked: {len(value)} characters)" if SENSITIVE_NAME.search(name) else None


Result = tuple[str, list[Source]]


def _run(ctx: RunContext[RunDeps], tool: str, args: dict[str, Any], body: Callable[[], Result]) -> str:
    """Run a tool body, record the call, and prefix the output with its id."""
    try:
        text, sources = body()
    except NotFound as e:
        text, sources = str(e), []
    except ApiException as e:
        if e.status == 401:
            raise ClusterAuthError(AUTH_HINT) from None
        # e.g. 403 from RBAC. Tell the model what happened in one line.
        text, sources = f"Kubernetes API error {e.status}: {e.reason}", []
    call_id = ctx.deps.recorder.record(tool, args, sources, ctx.tool_call_id)
    return f"[{call_id}]\n{text}"


def _names(items) -> list[str]:
    return sorted(i.metadata.name for i in items)


def _labels(labels: dict[str, str] | None) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted((labels or {}).items())) or "(none)"


def _selector(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def _when(event) -> datetime:
    t = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
    return t or datetime.min


# ---- object lookups shared by the tools ------------------------------------


def _read(read: Callable[[], Any], what: str, ns: str, existing: Callable[[], list[str]]):
    try:
        return read()
    except ApiException as e:
        if e.status != 404:
            raise
        names = existing()
        listing = ", ".join(names) if names else "none"
        raise NotFound(f"No {what} with that name in namespace {ns}. {what}s there: {listing}.") from None


def _pods_of(c: Cluster, ns: str, workload: str) -> list:
    """Pods of a workload: by its Deployment's selector, else by name prefix."""
    try:
        dep = c.apps.read_namespaced_deployment(workload, ns)
    except ApiException as e:
        if e.status != 404:
            raise
        dep = None
    if dep is not None and dep.spec.selector.match_labels:
        pods = c.core.list_namespaced_pod(ns, label_selector=_selector(dep.spec.selector.match_labels)).items
    else:
        pods = [p for p in c.core.list_namespaced_pod(ns).items if p.metadata.name.startswith(workload + "-")]
    if not pods:
        deps = _names(c.apps.list_namespaced_deployment(ns).items)
        raise NotFound(f"No pods found for workload {workload!r} in namespace {ns}. Deployments there: {', '.join(deps) or 'none'}.")
    return sorted(pods, key=lambda p: p.metadata.name)


# ---- tool bodies (plain functions of the cluster, tested with fakes) -------


def list_workloads(c: Cluster, ns: str) -> Result:
    deps = _names(c.apps.list_namespaced_deployment(ns).items)
    svcs = _names(c.core.list_namespaced_service(ns).items)
    cms = [n for n in _names(c.core.list_namespaced_config_map(ns).items) if n != "kube-root-ca.crt"]
    text = "\n".join(
        [
            f"Deployments: {', '.join(deps) or 'none'}",
            f"Services: {', '.join(svcs) or 'none'}",
            f"ConfigMaps: {', '.join(cms) or 'none'}",
        ]
    )
    return text, []


def _container_state(cs) -> str:
    parts = []
    if cs.state and cs.state.waiting:
        parts.append(f"waiting: {cs.state.waiting.reason}")
    elif cs.state and cs.state.terminated:
        parts.append(f"terminated: {cs.state.terminated.reason}")
    if cs.last_state and cs.last_state.terminated:
        t = cs.last_state.terminated
        parts.append(f"last terminated: {t.reason} (exit {t.exit_code})")
    return "; ".join(parts)


def list_pods(c: Cluster, ns: str) -> Result:
    pods = sorted(c.core.list_namespaced_pod(ns).items, key=lambda p: p.metadata.name)
    if not pods:
        return f"No pods in namespace {ns}.", []
    lines = []
    for p in pods:
        statuses = p.status.container_statuses or []
        ready = sum(1 for cs in statuses if cs.ready)
        restarts = sum(cs.restart_count or 0 for cs in statuses)
        line = f"{p.metadata.name}  phase={p.status.phase}  ready={ready}/{len(p.spec.containers)}  restarts={restarts}"
        states = [s for s in (_container_state(cs) for cs in statuses) if s]
        if states:
            line += "  " + " | ".join(states)
        line += f"  labels={_labels(p.metadata.labels)}"
        lines.append(line)
    return "\n".join(lines), [Source(kind="Pod", name=p.metadata.name, aspect="status") for p in pods]


def _tail(text: str | None) -> str:
    lines = (text or "").splitlines()[-LOG_LINES:]
    return "\n".join(_cap(line, MAX_LINE_CHARS) for line in lines) or "(empty)"


def _log(c: Cluster, ns: str, pod: str, container: str, previous: bool) -> str:
    try:
        return _tail(c.core.read_namespaced_pod_log(pod, ns, container=container, previous=previous, tail_lines=LOG_LINES))
    except ApiException as e:
        if e.status == 400:  # no previous container, or the container hasn't started
            return "(none)" if previous else "(not available: container not running)"
        raise


def get_pod_logs(c: Cluster, ns: str, workload: str) -> Result:
    pods = _pods_of(c, ns, workload)
    shown = pods[:MAX_LOG_PODS]
    blocks = []
    if len(pods) > len(shown):
        blocks.append(f"Showing {len(shown)} of {len(pods)} pods.")
    for p in shown:
        for container in p.spec.containers:
            # In a crash loop the useful line may be in either container, so read both.
            head = f"== {p.metadata.name} ({container.name})"
            blocks.append(f"{head} current:\n{_log(c, ns, p.metadata.name, container.name, previous=False)}")
            blocks.append(f"{head} previous:\n{_log(c, ns, p.metadata.name, container.name, previous=True)}")
    return "\n".join(blocks), [Source(kind="Pod", name=p.metadata.name, aspect="logs") for p in shown]


def get_events(c: Cluster, ns: str, workload: str | None) -> Result:
    events = c.core.list_namespaced_event(ns).items
    if workload:
        # The Deployment, its ReplicaSets (workload-<hash>) and pods (workload-<hash>-<id>).
        events = [e for e in events if e.involved_object.name == workload or e.involved_object.name.startswith(workload + "-")]
    events = sorted(events, key=_when)[-MAX_EVENTS:]
    if not events:
        scope = f" for {workload}" if workload else ""
        return f"No events{scope} in namespace {ns}.", []
    lines, sources = [], []
    for e in events:
        obj = e.involved_object
        count = f" (x{e.count})" if e.count and e.count > 1 else ""
        lines.append(f"{e.type} {e.reason} {obj.kind}/{obj.name}: {_cap(e.message or '', MAX_LINE_CHARS)}{count}")
        source = Source(kind=obj.kind, name=obj.name, aspect="events")
        if source not in sources:
            sources.append(source)
    return "\n".join(lines), sources


def get_service(c: Cluster, ns: str, name: str) -> Result:
    svc = _read(
        lambda: c.core.read_namespaced_service(name, ns),
        "Service",
        ns,
        lambda: _names(c.core.list_namespaced_service(ns).items),
    )
    selector = svc.spec.selector or {}
    ports = ", ".join(f"{p.port}->{p.target_port}/{p.protocol}" for p in svc.spec.ports or [])
    slices = c.discovery.list_namespaced_endpoint_slice(ns, label_selector=f"kubernetes.io/service-name={name}").items
    ready = []
    for s in slices:
        for ep in s.endpoints or []:
            if ep.conditions is None or ep.conditions.ready:
                target = ep.target_ref.name if ep.target_ref else "?"
                ready.extend(f"{addr} ({target})" for addr in ep.addresses or [])
    text = "\n".join(
        [
            f"Service {name}: type={svc.spec.type}  ports={ports or 'none'}",
            f"selector: {_labels(selector)}",
            f"ready endpoints: {', '.join(ready) if ready else 'none'}",
        ]
    )
    return text, [Source(kind="Service", name=name, aspect="spec"), Source(kind="Service", name=name, aspect="endpoints")]


def _probe(p) -> str:
    if p.http_get:
        how = f"httpGet path={p.http_get.path} port={p.http_get.port}"
    elif p.tcp_socket:
        how = f"tcpSocket port={p.tcp_socket.port}"
    elif p._exec:
        how = f"exec {' '.join(p._exec.command or [])}"
    else:
        how = "other"
    return f"{how} initialDelay={p.initial_delay_seconds or 0}s period={p.period_seconds or 10}s failureThreshold={p.failure_threshold or 3}"


def _env(e) -> str:
    if e.value is not None:
        return f"{e.name}={_masked(e.name, e.value) or _cap(e.value, 200)}"
    ref = e.value_from
    if ref and ref.config_map_key_ref:
        return f"{e.name} from ConfigMap {ref.config_map_key_ref.name} key {ref.config_map_key_ref.key}"
    if ref and ref.secret_key_ref:
        return f"{e.name} from Secret {ref.secret_key_ref.name} key {ref.secret_key_ref.key}"
    return f"{e.name} (from another source)"


def get_deployment(c: Cluster, ns: str, name: str) -> Result:
    dep = _read(
        lambda: c.apps.read_namespaced_deployment(name, ns),
        "Deployment",
        ns,
        lambda: _names(c.apps.list_namespaced_deployment(ns).items),
    )
    tpl = dep.spec.template
    lines = [
        f"Deployment {name}: replicas={dep.spec.replicas} ready={dep.status.ready_replicas or 0}",
        f"selector: {_labels(dep.spec.selector.match_labels)}",
        f"pod template labels: {_labels(tpl.metadata.labels)}",
    ]
    for ct in tpl.spec.containers:
        lines.append(f"container {ct.name}: image={ct.image}")
        for e in ct.env or []:
            lines.append(f"  env {_env(e)}")
        for ef in ct.env_from or []:
            if ef.config_map_ref:
                lines.append(f"  envFrom ConfigMap {ef.config_map_ref.name}")
            if ef.secret_ref:
                lines.append(f"  envFrom Secret {ef.secret_ref.name}")
        for kind in ("readiness", "liveness", "startup"):
            probe = getattr(ct, f"{kind}_probe")
            if probe:
                lines.append(f"  {kind}Probe: {_probe(probe)}")
    for vol in tpl.spec.volumes or []:
        if vol.config_map:
            lines.append(f"volume {vol.name}: ConfigMap {vol.config_map.name}")
    # The first line reports ready replicas, which is the Deployment's status, not its spec.
    return "\n".join(lines), [Source(kind="Deployment", name=name, aspect="spec"), Source(kind="Deployment", name=name, aspect="status")]


def get_configmap(c: Cluster, ns: str, name: str) -> Result:
    cm = _read(
        lambda: c.core.read_namespaced_config_map(name, ns),
        "ConfigMap",
        ns,
        lambda: [n for n in _names(c.core.list_namespaced_config_map(ns).items) if n != "kube-root-ca.crt"],
    )
    data = cm.data or {}
    if not data:
        text = f"ConfigMap {name}: no data"
    else:
        text = "\n".join(
            f"--- {key}:\n{_masked(key, value) or _cap(value, MAX_VALUE_CHARS)}" for key, value in sorted(data.items())
        )
    return text, [Source(kind="ConfigMap", name=name, aspect="data")]


# ---- the toolset the model sees ---------------------------------------------


def build_toolset(namespaces: tuple[str, ...]) -> FunctionToolset[RunDeps]:
    """The model-facing tools. `namespace` is an enum of the allowed namespaces."""
    Namespace = Literal[namespaces]  # type: ignore[valid-type]
    tools = FunctionToolset[RunDeps]()

    @tools.tool(name="list_workloads")
    def list_workloads_tool(ctx: RunContext[RunDeps], namespace: Namespace) -> str:  # type: ignore[valid-type]
        """List the names of the Deployments, Services and ConfigMaps in a namespace."""
        return _run(ctx, "list_workloads", {"namespace": namespace}, lambda: list_workloads(ctx.deps.cluster, namespace))

    @tools.tool(name="list_pods")
    def list_pods_tool(ctx: RunContext[RunDeps], namespace: Namespace) -> str:  # type: ignore[valid-type]
        """List the pods in a namespace: phase, ready containers, restarts, waiting or last termination reason, labels."""
        return _run(ctx, "list_pods", {"namespace": namespace}, lambda: list_pods(ctx.deps.cluster, namespace))

    @tools.tool(name="get_pod_logs")
    def get_pod_logs_tool(ctx: RunContext[RunDeps], namespace: Namespace, workload: str) -> str:  # type: ignore[valid-type]
        """Read the last 50 log lines of up to 3 pods of a workload (a Deployment name, e.g. "checkout"),
        from both the current and the previous container."""
        args = {"namespace": namespace, "workload": workload}
        return _run(ctx, "get_pod_logs", args, lambda: get_pod_logs(ctx.deps.cluster, namespace, workload))

    @tools.tool(name="get_events")
    def get_events_tool(ctx: RunContext[RunDeps], namespace: Namespace, workload: str | None = None) -> str:  # type: ignore[valid-type]
        """The last 20 events in a namespace, oldest first. With a workload (a Deployment name), only the events
        of that Deployment, its ReplicaSets and its pods."""
        args = {"namespace": namespace, "workload": workload}
        return _run(ctx, "get_events", args, lambda: get_events(ctx.deps.cluster, namespace, workload))

    @tools.tool(name="get_service")
    def get_service_tool(ctx: RunContext[RunDeps], namespace: Namespace, name: str) -> str:  # type: ignore[valid-type]
        """A Service's selector, ports and ready endpoints."""
        args = {"namespace": namespace, "name": name}
        return _run(ctx, "get_service", args, lambda: get_service(ctx.deps.cluster, namespace, name))

    @tools.tool(name="get_deployment")
    def get_deployment_tool(ctx: RunContext[RunDeps], namespace: Namespace, name: str) -> str:  # type: ignore[valid-type]
        """A Deployment's pod template: labels, image, env and envFrom, probes, ConfigMap volumes; replicas and ready."""
        args = {"namespace": namespace, "name": name}
        return _run(ctx, "get_deployment", args, lambda: get_deployment(ctx.deps.cluster, namespace, name))

    @tools.tool(name="get_configmap")
    def get_configmap_tool(ctx: RunContext[RunDeps], namespace: Namespace, name: str) -> str:  # type: ignore[valid-type]
        """A ConfigMap's data; each value capped at 2,000 characters."""
        args = {"namespace": namespace, "name": name}
        return _run(ctx, "get_configmap", args, lambda: get_configmap(ctx.deps.cluster, namespace, name))

    return tools
