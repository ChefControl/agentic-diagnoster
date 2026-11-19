"""The one fix the agent may apply: the body of its apply_fix tool.

The model can only ask: apply_fix needs a person's approval (a deferred tool,
see agent.py), and our code checks the fix against the allowlist before anyone
is asked. Once approved, this code applies exactly one field as the remediator
account and checks the result with fresh reads as the reader.

    allowlist -> (approval, in agent.py) -> apply -> verify
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kubernetes.client.exceptions import ApiException
from opentelemetry import trace

from .cluster import Cluster
from .contract import Diagnosis, ProposedFix

# The allowlist: one Deployment, one field, and the new value must be a plain path.
NAMESPACE = "payments"
TARGET = "deployment/payments-api"
FIELD = "readinessProbe.httpGet.path"
PATH_PATTERN = re.compile(r"^/[\w/-]*$")
VERIFY_TIMEOUT_S = 90
FIELD_MANAGER = "diagnoster-remediator"
VERIFY_INTERVAL_S = 3

Status = Literal["not_allowed", "stale", "fixed", "not_fixed"]


@dataclass
class Outcome:
    status: Status
    detail: str
    seen: list[str] = field(default_factory=list)


def check_allowed(diagnosis: Diagnosis | None) -> tuple[ProposedFix | None, str]:
    """The fix, if the diagnosis proposes exactly the allowed change; else the reason it doesn't."""
    if diagnosis is None:
        return None, "the run stopped without a diagnosis"
    action = diagnosis.next_action
    fix = action.proposed_fix
    if action.kind != "remediate" or fix is None:
        return None, f"the next action is {action.kind}, not remediate"
    if fix.target != TARGET:
        return None, f"only {TARGET} may be changed, not {fix.target}"
    if fix.field != FIELD:
        return None, f"only {FIELD} may be changed, not {fix.field}"
    if not PATH_PATTERN.match(fix.to):
        return None, f"the new path {fix.to!r} is not a plain path like /health"
    return fix, "allowed"


def patch_body(container: str, path: str) -> dict[str, Any]:
    """A strategic-merge patch of one container's readiness probe path, nothing else.

    Containers merge by name and the probe merges field by field, so the port
    and timings stay as they are.
    """
    return {"spec": {"template": {"spec": {"containers": [{"name": container, "readinessProbe": {"httpGet": {"path": path}}}]}}}}


def apply(remediator: Cluster, fix: ProposedFix) -> tuple[bool, str]:
    """Patch the probe path, after checking the live value still equals `from`."""
    name = TARGET.split("/", 1)[1]
    dep = remediator.apps.read_namespaced_deployment(name, NAMESPACE)
    live = {
        c.name: c.readiness_probe.http_get.path
        for c in dep.spec.template.spec.containers
        if c.readiness_probe and c.readiness_probe.http_get
    }
    container = next((c for c, path in live.items() if path == fix.from_), None)
    if container is None:
        found = ", ".join(f"{c}: {p}" for c, p in live.items()) or "no HTTP readiness probe"
        return False, f"the live probe path is not {fix.from_!r} ({found}); nothing was changed"
    # field_manager names us in managedFields, so the cluster shows who made the change.
    remediator.apps.patch_namespaced_deployment(name, NAMESPACE, patch_body(container, fix.to), field_manager=FIELD_MANAGER)
    return True, f"patched container {container}: {fix.from_} -> {fix.to}"


def check_once(reader: Cluster) -> tuple[bool, str]:
    """One fresh read: are all the Deployment's pods Ready, and does the Service have ready endpoints?"""
    name = TARGET.split("/", 1)[1]
    dep = reader.apps.read_namespaced_deployment(name, NAMESPACE)
    want = dep.spec.replicas or 0
    selector = ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))
    pods = reader.core.list_namespaced_pod(NAMESPACE, label_selector=selector).items
    ready = [p for p in pods if all(cs.ready for cs in p.status.container_statuses or [])] if pods else []
    slices = reader.discovery.list_namespaced_endpoint_slice(NAMESPACE, label_selector=f"kubernetes.io/service-name={name}").items
    endpoints = sum(
        len(ep.addresses or []) for s in slices for ep in s.endpoints or [] if ep.conditions is None or ep.conditions.ready
    )
    updated = dep.status.updated_replicas or 0
    # Old pods are still terminating during a rollout, so count must equal replicas too.
    ok = len(pods) == want and len(ready) == want and updated == want and endpoints >= want > 0
    return ok, f"pods ready {len(ready)}/{len(pods)} (want {want}), updated {updated}, ready endpoints {endpoints}"


def verify(
    reader: Cluster,
    timeout_s: float = VERIFY_TIMEOUT_S,
    interval_s: float = VERIFY_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, list[str]]:
    deadline = clock() + timeout_s
    seen = []
    while True:
        ok, line = check_once(reader)
        seen.append(line)
        if ok or clock() >= deadline:
            return ok, seen
        sleep(interval_s)


def fix(
    diagnosis: Diagnosis,
    *,
    remediator: Callable[[], Cluster],
    reader: Callable[[], Cluster],
    verify_fn: Callable[[Cluster], tuple[bool, list[str]]] = verify,
) -> Outcome:
    """Apply an approved fix and check it. The remediator account is loaded here and nowhere else."""
    tracer = trace.get_tracer("diagnoster")
    with tracer.start_as_current_span("remediate") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        outcome = _fix(diagnosis, tracer, remediator, reader, verify_fn)
        span.set_attribute("diagnoster.remediation.status", outcome.status)
        span.set_attribute("output.value", outcome.detail)
        if outcome.status in ("stale", "not_fixed"):
            span.set_status(trace.Status(trace.StatusCode.ERROR, outcome.detail))
    return outcome


def _fix(diagnosis, tracer, remediator, reader, verify_fn) -> Outcome:
    # Checked again here, though agent.py checks before asking: this is the code that writes.
    fix, reason = check_allowed(diagnosis)
    if fix is None:
        return Outcome("not_allowed", f"not applied: {reason}; it stays a recommendation")
    trace.get_current_span().set_attribute("input.value", f"{fix.target} {fix.field}: {fix.from_} -> {fix.to}")

    with tracer.start_as_current_span("apply") as span:
        try:
            applied, detail = apply(remediator(), fix)
        except ApiException as e:
            applied, detail = False, f"the API refused the change: {e.status} {e.reason}"
        except FileNotFoundError as e:  # no remediator kubeconfig
            applied, detail = False, str(e)
        span.set_attribute("output.value", detail)
    if not applied:
        return Outcome("stale", detail)

    with tracer.start_as_current_span("verify") as span:
        fixed, seen = verify_fn(reader())
        span.set_attribute("output.value", seen[-1] if seen else "")
    if fixed:
        return Outcome("fixed", f"{detail}; fixed: {seen[-1]}", seen)
    return Outcome("not_fixed", f"{detail}; not fixed after {VERIFY_TIMEOUT_S} s: {seen[-1] if seen else 'nothing seen'}", seen)
