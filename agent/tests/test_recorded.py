"""The recorded cluster: still matches the scenarios, and still shows each scenario's fault.

Both tests read the committed snapshot. When one fails after a manifest
change, re-record: see diagnoster/record.py.
"""

import pytest
import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from diagnoster import tools
from diagnoster.config import DEFAULT_NAMESPACES, REPO_ROOT
from diagnoster.recorded import SNAPSHOT as SNAPSHOT_FILE
from diagnoster.recorded import load

SNAPSHOT = load()
CLUSTER = SNAPSHOT.cluster
RE_RECORD = "the recorded cluster is out of date; re-record it with `uv run python -m diagnoster.record`"


def manifest_objects():
    for path in sorted((REPO_ROOT / "scenarios").glob("*/manifest.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                yield pytest.param(doc["kind"], doc["metadata"]["namespace"], doc["metadata"]["name"], id=f"{doc['kind']}/{doc['metadata']['name']}")


@pytest.mark.parametrize(("kind", "ns", "name"), list(manifest_objects()))
def test_every_object_in_the_scenario_manifests_is_in_the_snapshot(kind, ns, name):
    read = {
        "Deployment": CLUSTER.apps.read_namespaced_deployment,
        "Service": CLUSTER.core.read_namespaced_service,
        "ConfigMap": CLUSTER.core.read_namespaced_config_map,
    }[kind]
    try:
        read(name, ns)
    except ApiException:
        pytest.fail(f"{kind} {ns}/{name} is in a manifest but not in the snapshot: {RE_RECORD}")


def test_the_snapshot_covers_the_agents_namespaces():
    assert set(SNAPSHOT.namespaces) == set(DEFAULT_NAMESPACES), RE_RECORD


# Each scenario's fault must be visible through the tools, or a recorded eval can't be passed honestly.
def test_bad_probe_shows_the_probe_path_the_404s_and_the_route_the_app_serves():
    assert "readinessProbe: httpGet path=/healthz port=8080" in tools.get_deployment(CLUSTER, "payments", "payments-api")[0]
    assert "ready=0/1" in tools.list_pods(CLUSTER, "payments")[0]
    assert "Readiness probe failed: HTTP probe failed with statuscode: 404" in tools.get_events(CLUSTER, "payments", "payments-api")[0]
    assert "location = /health " in tools.get_configmap(CLUSTER, "payments", "payments-api-nginx")[0]
    assert "ready endpoints: none" in tools.get_service(CLUSTER, "payments", "payments-api")[0]


def test_missing_config_shows_the_crash_its_log_line_and_the_keys_that_exist():
    assert "restarts=" in tools.list_pods(CLUSTER, "shop")[0] and "CrashLoopBackOff" in tools.list_pods(CLUSTER, "shop")[0]
    logs, sources = tools.get_pod_logs(CLUSTER, "shop", "checkout")
    assert "FATAL: DATABASE_URL is not set" in logs
    assert "previous:\ncheckout-service" in logs  # the crashed container's log was recorded too
    assert [s.aspect for s in sources] == ["logs"]
    assert "envFrom ConfigMap checkout-config" in tools.get_deployment(CLUSTER, "shop", "checkout")[0]
    assert "--- DB_URL:" in tools.get_configmap(CLUSTER, "shop", "checkout-config")[0]


def test_selector_mismatch_shows_the_selector_the_labels_and_no_endpoints():
    service = tools.get_service(CLUSTER, "catalog", "catalog")[0]
    assert "selector: app=catalog-api" in service and "ready endpoints: none" in service
    assert "pod template labels: app=catalog,tier=backend" in tools.get_deployment(CLUSTER, "catalog", "catalog")[0]
    assert "upstream unreachable" in tools.get_pod_logs(CLUSTER, "catalog", "storefront")[0]


def test_healthy_shows_ready_pods_and_endpoints():
    pods = tools.list_pods(CLUSTER, "inventory")[0].splitlines()
    assert len(pods) == 3 and all("ready=1/1  restarts=0" in line for line in pods)
    assert "ready endpoints: 10." in tools.get_service(CLUSTER, "inventory", "inventory")[0]
    assert "sync ok" in tools.get_pod_logs(CLUSTER, "inventory", "inventory-sync")[0]


# The replay answers like the real client, so the tools can't tell the difference.
def test_objects_are_the_kubernetes_clients_own_models():
    pods = CLUSTER.core.list_namespaced_pod("payments")
    assert isinstance(pods, client.V1PodList) and isinstance(pods.items[0], client.V1Pod)
    assert isinstance(CLUSTER.core.list_namespaced_event("payments").items[0], client.CoreV1Event)


def test_a_label_selector_filters_like_the_api_server():
    assert len(CLUSTER.core.list_namespaced_pod("catalog", label_selector="app=catalog").items) == 2
    assert CLUSTER.core.list_namespaced_pod("catalog", label_selector="app=catalog-api").items == []
    slices = CLUSTER.discovery.list_namespaced_endpoint_slice("shop", label_selector="kubernetes.io/service-name=checkout").items
    assert len(slices) == 1


def test_missing_things_fail_with_the_api_servers_status_codes():
    with pytest.raises(ApiException) as not_found:
        CLUSTER.core.read_namespaced_service("no-such-service", "catalog")
    assert not_found.value.status == 404
    pod = CLUSTER.core.list_namespaced_pod("inventory").items[0]
    with pytest.raises(ApiException) as no_previous:  # it never restarted, so there is no previous container
        CLUSTER.core.read_namespaced_pod_log(pod.metadata.name, "inventory", container=pod.spec.containers[0].name, previous=True)
    assert no_previous.value.status == 400
    assert CLUSTER.apps.list_namespaced_deployment("kube-system").items == []


def test_an_unknown_name_gets_the_tools_usual_correction():
    # The tool turns the 404 into a message listing what exists, exactly as on a live cluster.
    with pytest.raises(tools.NotFound, match="Deployments there: catalog, storefront"):
        tools.get_deployment(CLUSTER, "catalog", "catalogue")


def test_the_snapshot_holds_no_bulky_or_private_fields():
    raw = SNAPSHOT_FILE.read_text()
    assert "managedFields" not in raw
    assert '"kind": "Secret"' not in raw
