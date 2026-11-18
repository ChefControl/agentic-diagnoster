"""`python -m diagnoster.record`: save the live cluster as the recorded cluster.

A maintainer command. Re-run it after a scenario's manifest changes, with all
four scenarios deployed and reproduced:

    cd ui && npm run scenarios -- deploy all && npm run scenarios -- check all --wait 180
    cd agent && uv run python -m diagnoster.record

It reads as the diagnoster-reader, so the file can only hold what the agent
itself may see (no Secrets). Per namespace it saves the raw API objects the
tools read, and the last log lines of every container, current and previous.
"""

import json
import sys
from datetime import UTC, datetime
from typing import Any

from kubernetes.client.exceptions import ApiException

from .cluster import Cluster, connect
from .config import load_settings
from .recorded import SNAPSHOT
from .tools import LOG_LINES


def _items(call, *args, **kwargs) -> list[dict[str, Any]]:
    """The raw JSON items of a list call, without the bulky managedFields."""
    items = json.loads(call(*args, _preload_content=False, **kwargs).data)["items"]
    for item in items:
        item["metadata"].pop("managedFields", None)
    return items


def _log(cluster: Cluster, ns: str, pod: str, container: str, previous: bool) -> str | None:
    try:
        return cluster.core.read_namespaced_pod_log(pod, ns, container=container, previous=previous, tail_lines=LOG_LINES)
    except ApiException as e:
        if e.status == 400:  # no previous container, or the container hasn't started
            return None
        raise


def record_namespace(cluster: Cluster, ns: str) -> dict[str, Any]:
    pods = _items(cluster.core.list_namespaced_pod, ns)
    logs = {}
    for pod in pods:
        name = pod["metadata"]["name"]
        for container in pod["spec"]["containers"]:
            logs[f"{name}/{container['name']}"] = {
                "current": _log(cluster, ns, name, container["name"], previous=False),
                "previous": _log(cluster, ns, name, container["name"], previous=True),
            }
    return {
        "deployments": _items(cluster.apps.list_namespaced_deployment, ns),
        "pods": pods,
        "services": _items(cluster.core.list_namespaced_service, ns),
        "configmaps": _items(cluster.core.list_namespaced_config_map, ns),
        "events": _items(cluster.core.list_namespaced_event, ns),
        "endpointslices": _items(cluster.discovery.list_namespaced_endpoint_slice, ns),
        "logs": logs,
    }


def main() -> int:
    settings = load_settings()
    try:
        cluster = connect(settings.reader_kubeconfig)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    namespaces = {ns: record_namespace(cluster, ns) for ns in settings.namespaces}
    empty = [ns for ns, saved in namespaces.items() if not saved["deployments"]]
    if empty:
        print(f"Nothing is deployed in: {', '.join(empty)}. Deploy every scenario first; nothing was written.", file=sys.stderr)
        return 1
    snapshot = {"recorded_at": datetime.now(UTC).isoformat(timespec="seconds"), "namespaces": namespaces}
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    counts = ", ".join(f"{ns}: {len(saved['pods'])} pods, {len(saved['events'])} events" for ns, saved in namespaces.items())
    print(f"Wrote {SNAPSHOT} ({SNAPSHOT.stat().st_size // 1024} KB). {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
