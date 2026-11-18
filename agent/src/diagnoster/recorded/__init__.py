"""A recorded cluster: the four scenarios as they looked at one moment, replayed from a file.

`cluster.json` holds the raw Kubernetes API objects that `diagnoster.record`
saved. `load()` gives a Cluster that answers the same client calls the tools
make, with the client's own model classes, so the tools run unchanged and
can't tell it from a live cluster.

Why: evals on the recorded cluster need no cluster at all, and every run sees
exactly the same facts, so two prompts or two models are compared fairly.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from ..cluster import Cluster

SNAPSHOT = Path(__file__).with_name("cluster.json")

# Snapshot key -> the client's list model for that kind.
LISTS = {
    "deployments": client.V1DeploymentList,
    "pods": client.V1PodList,
    "services": client.V1ServiceList,
    "configmaps": client.V1ConfigMapList,
    "events": client.CoreV1EventList,
    "endpointslices": client.V1EndpointSliceList,
}

Raw = dict[str, Any]


def _matches(item: Raw, selector: str | None) -> bool:
    """Equality selectors only ("a=b,c=d"): the only kind the tools send."""
    if not selector:
        return True
    labels = item["metadata"].get("labels") or {}
    wanted = dict(part.split("=", 1) for part in selector.split(","))
    return all(labels.get(k) == v for k, v in wanted.items())


class _Replay:
    def __init__(self, namespaces: dict[str, Raw]):
        self._namespaces = namespaces

    def _items(self, kind: str, ns: str, selector: str | None = None) -> list[Raw]:
        return [i for i in self._namespaces.get(ns, {}).get(kind, []) if _matches(i, selector)]

    def _list(self, kind: str, ns: str, selector: str | None = None):
        return LISTS[kind].from_dict({"items": self._items(kind, ns, selector)})

    def _read(self, kind: str, ns: str, name: str):
        for item in self._list(kind, ns).items:
            if item.metadata.name == name:
                return item
        raise ApiException(status=404, reason="Not Found")


class _Core(_Replay):
    def list_namespaced_pod(self, ns, label_selector=None):
        return self._list("pods", ns, label_selector)

    def read_namespaced_pod_log(self, name, ns, container=None, previous=False, tail_lines=None):
        saved = self._namespaces.get(ns, {}).get("logs", {}).get(f"{name}/{container}", {})
        text = saved.get("previous" if previous else "current")
        if text is None:  # what the API answers when that container never ran
            raise ApiException(status=400, reason="Bad Request")
        return "\n".join(text.splitlines()[-tail_lines:]) if tail_lines else text

    def list_namespaced_service(self, ns):
        return self._list("services", ns)

    def read_namespaced_service(self, name, ns):
        return self._read("services", ns, name)

    def list_namespaced_config_map(self, ns):
        return self._list("configmaps", ns)

    def read_namespaced_config_map(self, name, ns):
        return self._read("configmaps", ns, name)

    def list_namespaced_event(self, ns):
        return self._list("events", ns)


class _Apps(_Replay):
    def list_namespaced_deployment(self, ns):
        return self._list("deployments", ns)

    def read_namespaced_deployment(self, name, ns):
        return self._read("deployments", ns, name)


class _Discovery(_Replay):
    def list_namespaced_endpoint_slice(self, ns, label_selector=None):
        return self._list("endpointslices", ns, label_selector)


@dataclass(frozen=True)
class Snapshot:
    recorded_at: str
    namespaces: tuple[str, ...]
    cluster: Cluster


def load(path: Path = SNAPSHOT) -> Snapshot:
    if not path.is_file():
        raise FileNotFoundError(f"recorded cluster not found at {path}; record one with `python -m diagnoster.record`")
    data = json.loads(path.read_text())
    namespaces = data["namespaces"]
    cluster = Cluster(core=_Core(namespaces), apps=_Apps(namespaces), discovery=_Discovery(namespaces))
    return Snapshot(recorded_at=data["recorded_at"], namespaces=tuple(namespaces), cluster=cluster)
