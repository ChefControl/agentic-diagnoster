"""The diagnosis agent and one run of it.

The model drives the investigation with read-only tools and answers with a
Diagnosis. Our code wraps the run: limits, the tool-call record, the trace,
and a RunReport that always comes back, finished, stopped or waiting.

One tool can change the cluster: apply_fix. It needs a person's approval
(a PydanticAI deferred tool), so calling it ends the run with a request
instead of running it. Our code then answers the request:
- a fix outside the allowlist is denied before anyone is asked;
- where no one can approve (evals, the recorded cluster) it is denied;
- otherwise the run pauses (`on_pause`), and `resume` continues it with the
  person's answer. Approved, the tool applies and verifies the fix.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    ModelRetry,
    RunContext,
    ToolApproved,
    ToolDenied,
    UsageLimitExceeded,
    UsageLimits,
)
from pydantic_ai.exceptions import RunTimedOut, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage

from .cluster import Cluster
from .config import Settings
from .contract import Diagnosis, Remediation, RunReport, StopReason
from .remediation import VERIFY_TIMEOUT_S, check_allowed
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

APPLY_FIX = "apply_fix"


def build_agent(
    settings: Settings, model: Model | str | None = None, system_prompt: str | None = None
) -> Agent[RunDeps, Diagnosis]:
    agent = Agent(
        model or settings.model,
        output_type=[Diagnosis, DeferredToolRequests],
        deps_type=RunDeps,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        toolsets=[build_toolset(settings.namespaces)],
        retries={"tools": 2, "output": 2},
        name="diagnoster",
    )

    @agent.tool(name=APPLY_FIX, requires_approval=True)
    async def apply_fix(ctx: RunContext[RunDeps], diagnosis: Diagnosis, expected_effect: str) -> str:
        """Offer the user the proposed fix in your diagnosis; if they approve it, apply it and check that it worked.

        Call this when your next action is remediate, before your final answer, passing your full
        diagnosis. Then give your final answer, saying in the next action summary what happened to the fix.

        Args:
            diagnosis: Your full diagnosis; its next action is remediate with the proposed fix.
            expected_effect: What the change should do once applied, in a sentence or two, for the user deciding.
        """
        if ctx.deps.fixer is None:
            return "Nothing was changed: changes can't be made here."
        # remediation.fix blocks (a patch, then up to 90 s of fresh reads), so it runs off the event loop.
        outcome = await asyncio.to_thread(ctx.deps.fixer, diagnosis)
        ctx.deps.remediation = outcome.model_copy(update={"expected_effect": expected_effect})
        return f"{outcome.status}: {outcome.detail}"

    @agent.output_validator
    def _cites_real_calls(ctx: RunContext[RunDeps], output: Diagnosis | DeferredToolRequests) -> Diagnosis | DeferredToolRequests:
        if isinstance(output, DeferredToolRequests):
            return output
        # The one tool that can fix things is offered, not assumed: a remediate
        # answer means the agent asked the user through apply_fix first.
        if output.next_action.kind == "remediate" and ctx.deps.remediation is None:
            raise ModelRetry(
                f"Your next action is remediate, so call {APPLY_FIX} with this diagnosis and the expected effect "
                "before your final answer."
            )
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


@dataclass
class Paused:
    """A run waiting for a person to approve its fix. Kept by the server, in memory."""

    agent: Agent[RunDeps, Diagnosis | DeferredToolRequests]
    deps: RunDeps
    messages: list[ModelMessage]
    usage: RunUsage
    call: ToolCallPart
    diagnosis: Diagnosis
    settings: Settings
    model_label: str
    attributes: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None


# The model reads these as the tool's result, so they say what happened and
# nothing about why the question was asked.
DENIED = "The user denied the fix. Nothing was changed. Give your final answer."
ALREADY_DECIDED = "The user already decided on a fix in this run. Give your final answer."


def _proposal(call: ToolCallPart) -> tuple[Diagnosis, str | None] | str:
    try:
        args = call.args_as_dict()
        effect = args.get("expected_effect")
        return Diagnosis.model_validate(args.get("diagnosis")), effect if isinstance(effect, str) and effect.strip() else None
    except (ValidationError, ValueError) as e:
        return f"The diagnosis passed to {APPLY_FIX} is not valid: {e}"


def _answer_without_asking(requests: DeferredToolRequests, deps: RunDeps, no_approver: str | None):
    """Deny what our code can decide alone. Returns the results, or the one call to ask a person about."""
    results = DeferredToolResults()
    ask: tuple[ToolCallPart, Diagnosis] | None = None
    for call in requests.approvals:
        parsed = _proposal(call)
        if isinstance(parsed, str):
            # Our code may ask again (the rule in the output validator), so this is recorded too.
            deps.remediation = deps.remediation or Remediation(status="not_allowed", detail=parsed)
            results.approvals[call.tool_call_id] = ToolDenied(parsed)
            continue
        proposal, effect = parsed
        fix, reason = check_allowed(proposal)
        if deps.remediation is not None or ask is not None:
            results.approvals[call.tool_call_id] = ToolDenied(ALREADY_DECIDED)
        elif fix is None:
            detail = f"Not applied: {reason}. It stays a recommendation."
            deps.remediation = Remediation(
                status="not_allowed", detail=detail, fix=proposal.next_action.proposed_fix, expected_effect=effect
            )
            results.approvals[call.tool_call_id] = ToolDenied(f"{detail} Give your final answer.")
        elif no_approver is not None:
            deps.remediation = Remediation(status="not_asked", detail=no_approver, fix=fix, expected_effect=effect)
            results.approvals[call.tool_call_id] = ToolDenied(no_approver)
        else:
            deps.remediation = Remediation(status="asked", detail="Waiting for the user's answer.", fix=fix, expected_effect=effect)
            ask = (call, proposal)
    return results, ask


async def _drive(
    agent, deps: RunDeps, settings: Settings, *, question=None, messages=None, results=None, usage=None,
    no_approver: str | None, session_id: str | None, extra_time_s: float = 0,
):
    """Run until the agent answers or one fix needs a person. Returns (result, ask)."""
    limits = UsageLimits(request_limit=settings.request_limit, tool_calls_limit=settings.tool_calls_limit)
    while True:
        result = await agent.run(
            question,
            deps=deps,
            message_history=messages,
            deferred_tool_results=results,
            usage=usage,
            usage_limits=limits,
            timeout=settings.run_timeout_s + extra_time_s,
            conversation_id=session_id,
        )
        if not isinstance(result.output, DeferredToolRequests):
            return result, None
        results, ask = _answer_without_asking(result.output, deps, no_approver)
        if ask is not None:
            return result, (ask, results)
        question, messages, usage, extra_time_s = None, result.all_messages(), result.usage, 0


async def _traced_run(
    agent,
    name: str,
    question: str,
    settings: Settings,
    deps: RunDeps,
    model_label: str,
    attributes: Mapping[str, str] | None,
    session_id: str | None,
    tracer_provider: TracerProvider | None,
    raise_model_errors: bool,
    go: Callable,
    on_pause: Callable[[Paused], None] | None,
    after: Callable[[RunReport], None] | None = None,
) -> RunReport:
    tracer = (tracer_provider or trace.get_tracer_provider()).get_tracer("diagnoster")
    with tracer.start_as_current_span(name) as span:
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None  # None when tracing is off
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", question)
        span.set_attribute("diagnoster.model", model_label)
        span.set_attributes(dict(attributes or {}))
        if session_id:
            span.set_attribute("session.id", session_id)  # OpenInference's key for Phoenix sessions
        model_error: Exception | None = None
        try:
            result, pending = await go()
            if pending is None:
                report = RunReport(
                    status="complete", diagnosis=result.output, tool_calls=deps.recorder.calls,
                    trace_id=trace_id, remediation=deps.remediation,
                )
            else:
                (call, proposal), _ = pending
                report = RunReport(
                    status="awaiting_approval", diagnosis=proposal, tool_calls=deps.recorder.calls, trace_id=trace_id,
                    remediation=deps.remediation,
                )
                if on_pause is None:
                    raise RuntimeError("a run paused for approval with no one to keep it")
                on_pause(
                    Paused(
                        agent=agent, deps=deps, messages=result.all_messages(), usage=result.usage,
                        call=call, diagnosis=proposal, settings=settings, model_label=model_label,
                        attributes=dict(attributes or {}),
                        session_id=session_id,
                    )
                )
            span.set_status(Status(StatusCode.OK))
        except Exception as error:  # noqa: BLE001 - a run always ends in a report
            reason = _stop_reason(error)
            if reason == "model_error":
                model_error = error
                if not raise_model_errors:
                    log.warning("run stopped by %s: %s", type(error).__name__, error)
            report = RunReport(
                status="stopped", stop_reason=reason, tool_calls=deps.recorder.calls, trace_id=trace_id,
                remediation=deps.remediation,
            )
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, f"{reason}: {error}"))
        span.set_attribute("diagnoster.status", report.status)
        if report.stop_reason:
            span.set_attribute("diagnoster.stop_reason", report.stop_reason)
        if report.remediation:
            span.set_attribute("diagnoster.remediation.status", report.remediation.status)
        span.set_attribute("output.value", report.model_dump_json())
        span.set_attribute("output.mime_type", "application/json")
        if model_error is not None and raise_model_errors:
            raise model_error
        if after is not None:
            after(report)
    return report


async def diagnose(
    question: str,
    settings: Settings,
    cluster: Cluster,
    *,
    model: Model | str | None = None,
    system_prompt: str | None = None,
    attributes: Mapping[str, str] | None = None,
    session_id: str | None = None,
    no_approver: str | None = "No one can approve changes here, so nothing was changed. Give your final answer.",
    fixer: Callable[[Diagnosis], Remediation] | None = None,
    on_pause: Callable[[Paused], None] | None = None,
    tracer_provider: TracerProvider | None = None,
    after: Callable[[RunReport], None] | None = None,
    raise_model_errors: bool = False,
) -> RunReport:
    """Run the agent once. Never raises for a failed run: it returns a stopped report.

    `system_prompt` replaces the prompt in this file for one run: Phoenix's
    Playground sends the prompt being tried. `attributes` are added to the
    root span.

    `session_id` is the chat this run belongs to. PydanticAI's own spans carry
    its conversation id as their session id, so the chat's id is passed as
    that conversation id too: every span of the run then names one session,
    and Phoenix groups the chat's runs together.

    `no_approver`: when set, a fix the agent asks to apply is denied with this
    message (evals, the recorded cluster). When None, the run pauses instead:
    `on_pause` gets the paused run, the report says awaiting_approval, and
    `resume` continues it. `fixer` applies an approved fix.

    `after` runs inside the root span once the report exists.

    `raise_model_errors` is for evals: a rate limit or provider outage is then
    raised instead of reported, so it counts as a run that never happened, not
    as a wrong answer.
    """
    agent = build_agent(settings, model, system_prompt)
    deps = RunDeps(cluster=cluster, fixer=fixer)

    async def go():
        return await _drive(agent, deps, settings, question=question, no_approver=no_approver, session_id=session_id)

    return await _traced_run(
        agent, "diagnose", question, settings, deps, str(model or settings.model), attributes, session_id,
        tracer_provider, raise_model_errors, go, on_pause, after,
    )


async def resume(
    paused: Paused,
    approved: bool,
    *,
    tracer_provider: TracerProvider | None = None,
    raise_model_errors: bool = False,
) -> RunReport:
    """Continue a paused run with the person's answer. A new trace in the same session."""
    deps = paused.deps
    asked = deps.remediation
    effect = asked.expected_effect if asked else None
    results = DeferredToolResults()
    if approved:
        results.approvals[paused.call.tool_call_id] = ToolApproved()
    else:
        deps.remediation = Remediation(
            status="denied", detail="The user denied the fix. Nothing was changed.",
            fix=paused.diagnosis.next_action.proposed_fix, expected_effect=effect,
        )
        results.approvals[paused.call.tool_call_id] = ToolDenied(DENIED)

    async def go():
        # Approved, the tool also patches and verifies: give it that time on top.
        return await _drive(
            paused.agent, deps, paused.settings, messages=paused.messages, results=results, usage=paused.usage,
            no_approver=ALREADY_DECIDED, session_id=paused.session_id,
            extra_time_s=VERIFY_TIMEOUT_S if approved else 0,
        )

    attributes = paused.attributes | {"diagnoster.approval": "approved" if approved else "denied"}
    return await _traced_run(
        paused.agent, "resume", f"{'approve' if approved else 'deny'} the fix", paused.settings, deps,
        paused.model_label, attributes,
        paused.session_id, tracer_provider, raise_model_errors, go, None,
    )
