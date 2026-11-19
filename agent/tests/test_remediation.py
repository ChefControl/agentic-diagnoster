"""The apply_fix body on a fake cluster: only the allowed fix, only one field, then verified.

Approval itself is the agent's (a deferred tool); see test_agent.py."""

from unittest.mock import patch

import pytest
from kubernetes.client.exceptions import ApiException
from fakes import World, container, container_status, deployment, endpoint_slice, pod, probe
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from diagnoster import remediation, tracing
from diagnoster.contract import Diagnosis

FIX = {"target": "deployment/payments-api", "field": "readinessProbe.httpGet.path", "from": "/healthz", "to": "/health"}


def diagnosis(kind="remediate", **fix_changes) -> Diagnosis:
    fix = FIX | fix_changes
    return Diagnosis.model_validate(
        {
            "root_cause": {"category": "bad_probe", "resource": "deployment/payments-api", "explanation": "Probe path."},
            "evidence": [{"fact": "404 on /healthz", "tool_call_ids": ["tc_1"]}],
            "next_action": {"summary": "Fix the probe path.", "kind": kind, **({"proposed_fix": fix} if kind == "remediate" else {})},
        }
    )


def payments(path="/healthz", ready=False, endpoints=0, pods=2, updated=2) -> World:
    w = World()
    w.deployments = [
        deployment(
            "payments-api",
            {"app": "payments-api"},
            containers=[container("api", readiness=probe(path=path, port=8080))],
            replicas=2,
            ready=2 if ready else 0,
            updated=updated,
        )
    ]
    w.pods = [
        pod(f"payments-api-1-{i}", {"app": "payments-api"}, containers=("api",), statuses=[container_status(ready=ready)])
        for i in range(pods)
    ]
    w.slices = [endpoint_slice("payments-api", [f"payments-api-1-{i}" for i in range(endpoints)])]
    return w


# ---- allowlist ------------------------------------------------------------------


def test_the_allowed_fix_passes_the_allowlist():
    fix, reason = remediation.check_allowed(diagnosis())
    assert fix is not None and reason == "allowed"


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"target": "deployment/checkout"}, "only deployment/payments-api may be changed"),
        ({"target": "service/payments-api"}, "only deployment/payments-api may be changed"),
        ({"field": "livenessProbe.httpGet.path"}, "only readinessProbe.httpGet.path may be changed"),
        ({"field": "spec.replicas", "from": "2", "to": "0"}, "only readinessProbe.httpGet.path may be changed"),
        ({"to": "health"}, "is not a plain path"),
        ({"to": "/health; rm -rf /"}, "is not a plain path"),
        ({"to": "/health?x=1"}, "is not a plain path"),
        ({"to": "/health\n"}, "is not a plain path"),
        ({"to": "/"}, "is not a plain path"),
        ({"to": "//health"}, "is not a plain path"),
    ],
)
def test_the_allowlist_rejects_everything_else(changes, why):
    fix, reason = remediation.check_allowed(diagnosis(**changes))
    assert fix is None and why in reason


def test_a_recommendation_or_a_stopped_run_is_not_applied():
    assert remediation.check_allowed(diagnosis(kind="recommend"))[0] is None
    assert remediation.check_allowed(None)[1] == "the run stopped without a diagnosis"


# ---- apply ------------------------------------------------------------------------


def run(world, diag=None, verify_fn=None):
    opened = []

    def remediator():
        opened.append(True)
        return world.cluster()

    outcome = remediation.fix(
        diag or diagnosis(),
        remediator=remediator,
        reader=world.cluster,
        verify_fn=verify_fn or (lambda reader: (True, ["pods ready 2/2"])),
    )
    return outcome, opened


def test_a_disallowed_fix_is_never_applied_and_the_write_account_never_loaded():
    w = payments()
    outcome, opened = run(w, diag=diagnosis(target="deployment/checkout"))
    assert outcome.status == "not_allowed" and "stays a recommendation" in outcome.detail
    assert w.patches == [] and opened == []


def test_the_approved_fix_patches_exactly_the_one_field():
    w = payments()
    outcome, _ = run(w)
    assert outcome.status == "fixed"
    assert w.patches == [
        (
            "payments-api",
            "payments",
            {"spec": {"template": {"spec": {"containers": [{"name": "api", "readinessProbe": {"httpGet": {"path": "/health"}}}]}}}},
        )
    ]
    assert w.field_managers == ["diagnoster-remediator"]


def test_a_live_value_that_changed_stops_without_patching():
    w = payments(path="/ready")
    outcome, _ = run(w)
    assert outcome.status == "stale" and "api: /ready" in outcome.detail
    assert w.patches == []


def test_a_refused_patch_is_failed_not_stale():
    def refusing():
        raise ApiException(status=403, reason="Forbidden")

    outcome = remediation.fix(diagnosis(), remediator=refusing, reader=payments().cluster)
    assert outcome.status == "failed" and "403 Forbidden" in outcome.detail and "nothing was changed" in outcome.detail


def test_a_missing_write_account_is_failed():
    def missing():
        raise FileNotFoundError("no remediator kubeconfig")

    outcome = remediation.fix(diagnosis(), remediator=missing, reader=payments().cluster)
    assert outcome.status == "failed"


def test_an_error_while_checking_still_reports_the_applied_patch():
    def expired(reader):
        raise ApiException(status=401, reason="Unauthorized")

    w = payments()
    outcome, _ = run(w, verify_fn=expired)
    assert outcome.status == "not_fixed"
    assert "patched container api" in outcome.detail and "could not check the result: 401 Unauthorized" in outcome.detail
    assert len(w.patches) == 1


def test_a_failed_verification_is_reported():
    outcome, _ = run(payments(), verify_fn=lambda reader: (False, ["pods ready 0/2"]))
    assert outcome.status == "not_fixed" and "pods ready 0/2" in outcome.detail


# ---- verify -----------------------------------------------------------------------


def test_check_once_passes_only_when_all_pods_are_ready_and_endpoints_exist():
    assert remediation.check_once(payments(ready=True, endpoints=2).cluster())[0]
    assert not remediation.check_once(payments(ready=False, endpoints=0).cluster())[0]
    assert not remediation.check_once(payments(ready=True, endpoints=0).cluster())[0]
    # Mid-rollout: an old pod is still there, or not every replica is updated yet.
    assert not remediation.check_once(payments(ready=True, endpoints=2, pods=3).cluster())[0]
    assert not remediation.check_once(payments(ready=True, endpoints=2, updated=1).cluster())[0]


def test_verify_polls_until_fixed():
    w = payments()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:  # the rollout finishes after two waits
            w.pods = payments(ready=True).pods
            w.slices = payments(endpoints=2).slices

    fixed, seen = remediation.verify(w.cluster(), timeout_s=90, interval_s=3, sleep=sleep, clock=lambda: 0)
    assert fixed and len(seen) == 3 and sleeps == [3, 3]


def test_verify_gives_up_at_the_deadline():
    times = iter([0, 30, 60, 91])
    fixed, seen = remediation.verify(payments().cluster(), timeout_s=90, sleep=lambda s: None, clock=lambda: next(times))
    assert not fixed and len(seen) == 3


# ---- tracing ------------------------------------------------------------------------


def test_the_fix_is_traced_with_apply_and_verify_spans():
    exporter = InMemorySpanExporter()
    provider = tracing.make_provider(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with patch.object(remediation.trace, "get_tracer", return_value=tracer):
        with tracer.start_as_current_span("diagnose"):
            run(payments())
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"diagnose", "remediate", "apply", "verify"} <= set(spans)
    assert spans["remediate"].parent.span_id == spans["diagnose"].context.span_id
    for child in ("apply", "verify"):
        assert spans[child].parent.span_id == spans["remediate"].context.span_id
    assert spans["remediate"].attributes["diagnoster.remediation.status"] == "fixed"
