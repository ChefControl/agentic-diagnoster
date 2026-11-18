"""A fake cluster for tests: the client calls the tools make, answered from plain objects.

The tools only read attributes, so SimpleNamespace stands in for the
Kubernetes client's model classes. One namespace; the `ns` argument is ignored.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace as N

from kubernetes.client.exceptions import ApiException

from diagnoster.cluster import Cluster

T0 = datetime(2026, 11, 18, 12, 0, 0)


def meta(name, labels=None, minutes=0):
    return N(name=name, labels=labels, creation_timestamp=T0 + timedelta(minutes=minutes))


def container_status(ready=True, restarts=0, waiting=None, last_reason=None, exit_code=1):
    state = N(waiting=N(reason=waiting) if waiting else None, terminated=None)
    last = N(terminated=N(reason=last_reason, exit_code=exit_code) if last_reason else None)
    return N(ready=ready, restart_count=restarts, state=state, last_state=last)


def pod(name, labels, phase="Running", containers=("app",), statuses=None):
    statuses = statuses if statuses is not None else [container_status() for _ in containers]
    return N(
        metadata=meta(name, labels),
        spec=N(containers=[N(name=c) for c in containers]),
        status=N(phase=phase, container_statuses=statuses),
    )


def probe(path=None, port=80, tcp=None, cmd=None):
    return N(
        http_get=N(path=path, port=port) if path else None,
        tcp_socket=N(port=tcp) if tcp else None,
        _exec=N(command=cmd) if cmd else None,
        initial_delay_seconds=1,
        period_seconds=5,
        failure_threshold=3,
    )


def env(name, value=None, cm=None, key=None):
    ref = N(config_map_key_ref=N(name=cm, key=key), secret_key_ref=None) if cm else None
    return N(name=name, value=value, value_from=ref)


def container(name="app", image="nginx:1.27-alpine", env_=None, env_from=None, readiness=None):
    return N(
        name=name,
        image=image,
        env=env_,
        env_from=[N(config_map_ref=N(name=c), secret_ref=None) for c in env_from or []],
        readiness_probe=readiness,
        liveness_probe=None,
        startup_probe=None,
    )


def deployment(name, match, template_labels=None, containers=None, replicas=1, ready=1, volumes=None, updated=None):
    return N(
        metadata=meta(name),
        spec=N(
            replicas=replicas,
            selector=N(match_labels=match),
            template=N(
                metadata=N(labels=template_labels or match),
                spec=N(containers=containers or [container()], volumes=volumes),
            ),
        ),
        status=N(ready_replicas=ready, updated_replicas=replicas if updated is None else updated),
    )


def service(name, selector, port=80, target=8080):
    return N(metadata=meta(name), spec=N(type="ClusterIP", selector=selector, ports=[N(port=port, target_port=target, protocol="TCP")]))


def endpoint_slice(service_name, ready_pods):
    eps = [N(addresses=[f"10.0.0.{i + 1}"], conditions=N(ready=True), target_ref=N(name=p)) for i, p in enumerate(ready_pods)]
    return N(metadata=meta(f"{service_name}-abc", {"kubernetes.io/service-name": service_name}), endpoints=eps)


def event(kind, name, reason, message, minutes, type_="Warning", count=1):
    return N(
        metadata=meta(f"{name}.{minutes}", minutes=minutes),
        involved_object=N(kind=kind, name=name),
        type=type_,
        reason=reason,
        message=message,
        count=count,
        last_timestamp=T0 + timedelta(minutes=minutes),
        event_time=None,
    )


def configmap(name, data):
    return N(metadata=meta(name), data=data)


def _matches(labels, selector):
    if not selector:
        return True
    wanted = dict(part.split("=", 1) for part in selector.split(","))
    return all((labels or {}).get(k) == v for k, v in wanted.items())


def _find(items, name):
    for item in items:
        if item.metadata.name == name:
            return item
    raise ApiException(status=404, reason="Not Found")


class _Core:
    def __init__(self, w):
        self.w = w

    def list_namespaced_pod(self, ns, label_selector=None):
        return N(items=[p for p in self.w.pods if _matches(p.metadata.labels, label_selector)])

    def read_namespaced_pod_log(self, name, ns, container=None, previous=False, tail_lines=None):
        key = (name, previous)
        if key not in self.w.logs:
            raise ApiException(status=400, reason="Bad Request")
        return self.w.logs[key]

    def list_namespaced_service(self, ns):
        return N(items=self.w.services)

    def read_namespaced_service(self, name, ns):
        return _find(self.w.services, name)

    def list_namespaced_config_map(self, ns):
        return N(items=self.w.configmaps)

    def read_namespaced_config_map(self, name, ns):
        return _find(self.w.configmaps, name)

    def list_namespaced_event(self, ns):
        return N(items=self.w.events)


class _Apps:
    def __init__(self, w):
        self.w = w

    def list_namespaced_deployment(self, ns):
        return N(items=self.w.deployments)

    def read_namespaced_deployment(self, name, ns):
        return _find(self.w.deployments, name)

    def patch_namespaced_deployment(self, name, ns, body, field_manager=None):
        self.w.patches.append((name, ns, body))
        self.w.field_managers.append(field_manager)


class _Discovery:
    def __init__(self, w):
        self.w = w

    def list_namespaced_endpoint_slice(self, ns, label_selector=None):
        return N(items=[s for s in self.w.slices if _matches(s.metadata.labels, label_selector)])


class World:
    """Everything in the fake namespace. Tests change the lists directly."""

    def __init__(self):
        self.pods, self.deployments, self.services, self.slices = [], [], [], []
        self.configmaps, self.events = [], []
        self.logs: dict[tuple[str, bool], str] = {}
        self.patches: list = []
        self.field_managers: list = []

    def cluster(self) -> Cluster:
        return Cluster(core=_Core(self), apps=_Apps(self), discovery=_Discovery(self))


def selector_mismatch_world() -> World:
    """A Service that selects app=catalog-api while the pods are labeled app=catalog."""
    w = World()
    w.deployments = [deployment("catalog", {"app": "catalog"}), deployment("storefront", {"app": "storefront"})]
    w.pods = [
        pod("catalog-7d9f-aaaaa", {"app": "catalog", "pod-template-hash": "7d9f"}),
        pod("catalog-7d9f-bbbbb", {"app": "catalog", "pod-template-hash": "7d9f"}),
        pod("storefront-5c4b-ccccc", {"app": "storefront", "pod-template-hash": "5c4b"}),
    ]
    w.services = [service("catalog", {"app": "catalog-api"})]
    w.slices = [endpoint_slice("catalog", [])]
    w.configmaps = [configmap("kube-root-ca.crt", {"ca.crt": "..."}), configmap("storefront-config", {"URL": "http://catalog"})]
    w.logs = {("storefront-5c4b-ccccc", False): "GET /products -> connection refused\n" * 3}
    return w
