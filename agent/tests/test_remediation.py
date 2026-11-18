"""The remediation workflow on a fake cluster: only the allowed fix, only after "y", only one field."""

from unittest.mock import patch

import pytest
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
    ],
)
def test_the_allowlist_rejects_everything_else(changes, why):
    fix, reason = remediation.check_allowed(diagnosis(**changes))
    assert fix is None and why in reason


def test_a_recommendation_or_a_stopped_run_is_not_applied():
    assert remediation.check_allowed(diagnosis(kind="recommend"))[0] is None
    assert remediation.check_allowed(None)[1] == "the run stopped without a diagnosis"


# ---- explain, approve, apply ----------------------------------------------------


def run(world, answer="y", diag=None, verify_fn=None):
    said, asked, opened = [], [], []

    def remediator():
        opened.append(True)
        return world.cluster()

    outcome = remediation.remediate(
        diag or diagnosis(),
        remediator=remediator,
        reader=world.cluster(),
        ask=lambda q: asked.append(q) or answer,
        say=said.append,
        verify_fn=verify_fn or (lambda reader: (True, ["pods ready 2/2"])),
    )
    return outcome, said, asked, opened


@pytest.mark.parametrize("answer", ["n", "", "N", "yes", " no "])
def test_anything_but_y_applies_nothing(answer):
    w = payments()
    outcome, said, asked, opened = run(w, answer)
    assert outcome.status == "declined"
    assert w.patches == [] and opened == []  # the remediator account is never even loaded
    assert asked == ["Apply? [y/N] "]


def test_the_change_is_explained_before_asking():
    outcome, said, asked, _ = run(payments(), "n")
    assert "deployment/payments-api  readinessProbe.httpGet.path: /healthz -> /health" in said[0]
    assert "pods become Ready" in said[0]


def test_a_disallowed_fix_never_asks():
    w = payments()
    outcome, said, asked, opened = run(w, diag=diagnosis(target="deployment/checkout"))
    assert outcome.status == "not_allowed" and "stays a recommendation" in outcome.detail
    assert asked == [] and w.patches == [] and opened == []


def test_y_patches_exactly_the_one_field():
    w = payments()
    outcome, *_ = run(w, " Y ")
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
    outcome, *_ = run(w)
    assert outcome.status == "stale" and "api: /ready" in outcome.detail
    assert w.patches == []


def test_a_failed_verification_is_reported():
    outcome, *_ = run(payments(), verify_fn=lambda reader: (False, ["pods ready 0/2"]))
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


def test_the_workflow_is_traced_with_approve_apply_and_verify_spans():
    exporter = InMemorySpanExporter()
    provider = tracing.make_provider(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with patch.object(remediation.trace, "get_tracer", return_value=tracer):
        with tracer.start_as_current_span("diagnose"):
            run(payments())
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"diagnose", "remediate", "approve", "apply", "verify"} <= set(spans)
    assert spans["remediate"].parent.span_id == spans["diagnose"].context.span_id
    for child in ("approve", "apply", "verify"):
        assert spans[child].parent.span_id == spans["remediate"].context.span_id
    assert spans["approve"].attributes["diagnoster.approval.answer"] == "y"
    assert spans["remediate"].attributes["diagnoster.remediation.status"] == "fixed"
