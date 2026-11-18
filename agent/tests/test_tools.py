"""The tools against a fake cluster: what they print and which sources they record."""

from fakes import (
    World,
    configmap,
    container,
    container_status,
    deployment,
    env,
    event,
    pod,
    probe,
    selector_mismatch_world,
)

from diagnoster import tools
from diagnoster.contract import Source


def test_list_workloads_names_everything_but_the_root_ca():
    text, sources = tools.list_workloads(selector_mismatch_world().cluster(), "catalog")
    assert "Deployments: catalog, storefront" in text
    assert "Services: catalog" in text
    assert "ConfigMaps: storefront-config" in text and "kube-root-ca" not in text
    assert sources == []


def test_list_pods_shows_state_and_labels_with_one_source_per_pod():
    w = selector_mismatch_world()
    w.pods.append(
        pod("checkout-1-xyz", {"app": "checkout"}, statuses=[container_status(False, 4, "CrashLoopBackOff", "Error", 1)])
    )
    text, sources = tools.list_pods(w.cluster(), "catalog")
    assert "catalog-7d9f-aaaaa  phase=Running  ready=1/1  restarts=0" in text
    assert "labels=app=catalog,pod-template-hash=7d9f" in text
    assert "ready=0/1  restarts=4  waiting: CrashLoopBackOff; last terminated: Error (exit 1)" in text
    assert [s.name for s in sources] == ["catalog-7d9f-aaaaa", "catalog-7d9f-bbbbb", "checkout-1-xyz", "storefront-5c4b-ccccc"]
    assert {s.aspect for s in sources} == {"status"} and {s.kind for s in sources} == {"Pod"}


def test_service_without_endpoints_shows_the_mismatch():
    text, sources = tools.get_service(selector_mismatch_world().cluster(), "catalog", "catalog")
    assert "selector: app=catalog-api" in text
    assert "matching" not in text  # the model compares the selector with pod labels itself
    assert "ready endpoints: none" in text
    assert sources == [Source(kind="Service", name="catalog", aspect="spec"), Source(kind="Service", name="catalog", aspect="endpoints")]


def test_unknown_name_lists_what_exists_and_records_no_source():
    c = selector_mismatch_world().cluster()
    for body, what in [
        (lambda: tools.get_service(c, "catalog", "catalogue"), "Services there: catalog."),
        (lambda: tools.get_deployment(c, "catalog", "nope"), "Deployments there: catalog, storefront."),
        (lambda: tools.get_configmap(c, "catalog", "nope"), "ConfigMaps there: storefront-config."),
        (lambda: tools.get_pod_logs(c, "catalog", "nope"), "Deployments there: catalog, storefront."),
    ]:
        try:
            body()
        except tools.NotFound as e:
            assert what in str(e)
        else:
            raise AssertionError("expected NotFound")


def test_deployment_shows_probes_env_and_config_sources():
    w = World()
    w.deployments = [
        deployment(
            "payments-api",
            {"app": "payments-api"},
            containers=[
                container(
                    "api",
                    env_=[env("MODE", "live"), env("DB_URL", cm="checkout-config", key="db_url")],
                    env_from=["shared-config"],
                    readiness=probe(path="/healthz", port=80),
                )
            ],
            replicas=2,
            ready=0,
        )
    ]
    text, sources = tools.get_deployment(w.cluster(), "payments", "payments-api")
    assert "replicas=2 ready=0" in text
    assert "env MODE=live" in text
    assert "env DB_URL from ConfigMap checkout-config key db_url" in text
    assert "envFrom ConfigMap shared-config" in text
    assert "readinessProbe: httpGet path=/healthz port=80" in text
    assert sources == [Source(kind="Deployment", name="payments-api", aspect="spec")]


def test_configmap_values_are_capped():
    w = World()
    w.configmaps = [configmap("big", {"nginx.conf": "x" * 5000, "small": "ok"})]
    text, sources = tools.get_configmap(w.cluster(), "payments", "big")
    assert "--- small:\nok" in text
    assert "x" * tools.MAX_VALUE_CHARS + "... [3000 more characters]" in text
    assert sources == [Source(kind="ConfigMap", name="big", aspect="data")]


def test_logs_resolve_pods_by_the_deployment_selector_and_read_previous_too():
    w = World()
    w.deployments = [deployment("checkout", {"app": "checkout"})]
    w.pods = [pod(f"checkout-1-{i}", {"app": "checkout"}) for i in "abcd"] + [pod("checkout-db-0", {"app": "db"})]
    w.logs = {("checkout-1-a", False): "starting\n", ("checkout-1-a", True): "FATAL: DB_URL is not set\n"}
    text, sources = tools.get_pod_logs(w.cluster(), "shop", "checkout")
    assert "Showing 3 of 4 pods." in text
    assert "checkout-db-0" not in text  # name prefix alone would wrongly include it
    assert "== checkout-1-a (app) previous:\nFATAL: DB_URL is not set" in text
    assert "== checkout-1-b (app) previous:\n(none)" in text
    assert "== checkout-1-b (app) current:\n(not available: container not running)" in text
    assert [s.name for s in sources] == ["checkout-1-a", "checkout-1-b", "checkout-1-c"]
    assert {s.aspect for s in sources} == {"logs"}


def test_logs_fall_back_to_the_name_prefix_without_a_deployment():
    w = World()
    w.pods = [pod("sync-job-abc", {"job": "sync"})]
    w.logs = {("sync-job-abc", False): "done\n"}
    text, sources = tools.get_pod_logs(w.cluster(), "inventory", "sync-job")
    assert "done" in text and [s.name for s in sources] == ["sync-job-abc"]


def test_log_lines_are_tailed_and_capped():
    w = World()
    w.pods = [pod("web-1-a", {"app": "web"})]
    w.logs = {("web-1-a", False): "\n".join(f"line {i}" for i in range(80)) + "\n" + "y" * 1000}
    text, _ = tools.get_pod_logs(w.cluster(), "shop", "web")
    assert "line 29" not in text and "line 31" in text  # the last 50 lines
    assert "y" * tools.MAX_LINE_CHARS + "... [700 more characters]" in text


def test_events_for_a_workload_are_oldest_first_with_one_source_per_object():
    w = World()
    w.events = [
        event("Pod", "checkout-1-a", "BackOff", "Back-off restarting failed container", 5, count=7),
        event("Deployment", "checkout", "ScalingReplicaSet", "Scaled up", 1, type_="Normal"),
        event("Pod", "checkout-1-a", "Started", "Started container", 2, type_="Normal"),
        event("Pod", "inventory-1-a", "Pulled", "Pulled image", 3, type_="Normal"),
    ]
    text, sources = tools.get_events(w.cluster(), "shop", "checkout")
    lines = text.splitlines()
    assert lines[0].startswith("Normal ScalingReplicaSet Deployment/checkout")
    assert lines[-1] == "Warning BackOff Pod/checkout-1-a: Back-off restarting failed container (x7)"
    assert "inventory" not in text
    assert sources == [Source(kind="Deployment", name="checkout", aspect="events"), Source(kind="Pod", name="checkout-1-a", aspect="events")]


def test_events_keep_only_the_last_twenty():
    w = World()
    w.events = [event("Pod", "web-1-a", "Tick", f"n{i}", i, type_="Normal") for i in range(30)]
    text, _ = tools.get_events(w.cluster(), "shop", None)
    assert len(text.splitlines()) == tools.MAX_EVENTS
    assert "n9" not in text.split() and "n10" in text and "n29" in text


def test_recorder_numbers_calls_in_order():
    r = tools.Recorder()
    assert r.record("list_pods", {"namespace": "shop"}, [], "call_1") == "tc_1"
    assert r.record("get_service", {"namespace": "shop", "name": "x"}, [], None) == "tc_2"
    assert r.ids() == {"tc_1", "tc_2"} and r.calls[0].provider_id == "call_1"
