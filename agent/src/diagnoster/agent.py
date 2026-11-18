"""The diagnosis agent and one run of it.

The model drives the investigation with read-only tools and answers with a
Diagnosis. Our code wraps the run: limits, the tool-call record, the trace,
and a RunReport that always comes back, finished or stopped.
"""

import logging

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent, ModelRetry, RunContext, UsageLimitExceeded, UsageLimits
from pydantic_ai.exceptions import RunTimedOut, UnexpectedModelBehavior
from pydantic_ai.models import Model

from .cluster import Cluster
from .config import Settings
from .contract import Diagnosis, RunReport, StopReason
from .tools import RunDeps, build_toolset

log = logging.getLogger(__name__)

# A general method, not a list of our scenarios' symptoms: naming the faults the
# evals test would score well on them and badly on everything else.
SYSTEM_PROMPT = """\
You are a Kubernetes diagnostician. A user reports a problem, or asks whether something is
wrong, in a cluster. Find the root cause using only your read-only tools.

How to work:
- Get an overview of the namespace in the question before narrowing down.
- Form a hypothesis from what you see, then look for evidence that confirms or rules it out.
- Separate symptom from cause: keep asking why until you reach what a fix would change.
- Prefer the explanation that accounts for every symptom you observed.
- Confirm the cause in the tool output before concluding. If the evidence stays
  inconclusive, say so and use category other rather than guessing.

Rules:
- Every tool output starts with an id like [tc_3]. Cite those ids as evidence; cite only ids you were shown.
- State only facts the tool output shows. Never invent names, values or messages.
- If nothing is wrong, use category none, resource null and next action kind none.
- Use next action kind remediate only for a single field change you can state exactly (target, field,
  from, to) with the current value seen in a tool output. Otherwise use recommend.
"""


def build_agent(settings: Settings, model: Model | str | None = None) -> Agent[RunDeps, Diagnosis]:
    agent = Agent(
        model or settings.model,
        output_type=Diagnosis,
        deps_type=RunDeps,
        system_prompt=SYSTEM_PROMPT,
        toolsets=[build_toolset(settings.namespaces)],
        retries={"tools": 2, "output": 2},
        name="diagnoster",
    )

    @agent.output_validator
    def _cites_real_calls(ctx: RunContext[RunDeps], output: Diagnosis) -> Diagnosis:
        known = ctx.deps.recorder.ids()
        cited = {cid for item in output.evidence for cid in item.tool_call_ids}
        unknown = sorted(cited - known)
        if unknown:
            shown = ", ".join(sorted(known, key=lambda i: int(i.split("_")[1]))) or "none"
            raise ModelRetry(f"You cited {', '.join(unknown)}, which no tool call has. The ids you were shown: {shown}.")
        return output

    return agent


def _stop_reason(error: Exception) -> StopReason:
    if isinstance(error, UsageLimitExceeded):
        return "tool_call_limit" if "tool_calls_limit" in str(error) else "request_limit"
    if isinstance(error, RunTimedOut):
        return "timeout"
    if isinstance(error, UnexpectedModelBehavior):
        return "invalid_output"
    return "model_error"


async def diagnose(
    question: str,
    settings: Settings,
    cluster: Cluster,
    *,
    model: Model | str | None = None,
    tracer_provider: TracerProvider | None = None,
) -> RunReport:
    """Run the agent once. Never raises for a failed run: it returns a stopped report."""
    tracer = (tracer_provider or trace.get_tracer_provider()).get_tracer("diagnoster")
    agent = build_agent(settings, model)
    deps = RunDeps(cluster=cluster)
    limits = UsageLimits(request_limit=settings.request_limit, tool_calls_limit=settings.tool_calls_limit)

    with tracer.start_as_current_span("diagnose") as span:
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None  # None when tracing is off
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", question)
        span.set_attribute("diagnoster.model", str(model or settings.model))
        try:
            result = await agent.run(question, deps=deps, usage_limits=limits, timeout=settings.run_timeout_s)
            report = RunReport(status="complete", diagnosis=result.output, tool_calls=deps.recorder.calls, trace_id=trace_id)
            span.set_status(Status(StatusCode.OK))
        except Exception as error:  # noqa: BLE001 - a run always ends in a report
            reason = _stop_reason(error)
            if reason == "model_error":
                log.warning("run stopped by %s: %s", type(error).__name__, error)
            report = RunReport(status="stopped", stop_reason=reason, tool_calls=deps.recorder.calls, trace_id=trace_id)
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, f"{reason}: {error}"))
        span.set_attribute("diagnoster.status", report.status)
        if report.stop_reason:
            span.set_attribute("diagnoster.stop_reason", report.stop_reason)
        span.set_attribute("output.value", report.model_dump_json())
        span.set_attribute("output.mime_type", "application/json")
    return report
