"""The agent loop with a scripted model: no network, no API key.

FunctionModel plays the model: each test scripts which tools it calls and what
it answers, and the real agent code runs around it.
"""

import asyncio
import json

import pytest
from fakes import selector_mismatch_world
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from diagnoster import tracing
from diagnoster.agent import diagnose, resume
from diagnoster.config import Settings
from diagnoster.contract import Remediation, Source

SETTINGS = Settings(namespaces=("catalog", "shop"))
QUESTION = "The storefront in namespace catalog can't reach the catalog service. Why?"


def answer(*cited: str) -> dict:
    return {
        "root_cause": {
            "category": "selector_mismatch",
            "resource": "service/catalog",
            "explanation": "The Service selects app=catalog-api; the pods are labeled app=catalog.",
        },
        "evidence": [{"fact": "Service catalog has no endpoints", "tool_call_ids": list(cited)}],
        "next_action": {"summary": "Change the Service selector to app=catalog.", "kind": "recommend"},
    }


def scripted(*steps):
    """A model that plays the given steps in order: (tool name, args) per request."""
    calls = iter(steps)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        name, args = next(calls)
        if name == "final":
            name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(name, json.dumps(args), tool_call_id=f"prov_{len(messages)}")])

    return FunctionModel(model)


def loop_forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "catalog"})])


async def run(model, settings=SETTINGS, **kw):
    return await diagnose(QUESTION, settings, selector_mismatch_world().cluster(), model=model, **kw)


async def test_a_scripted_run_returns_a_complete_report_with_recorded_calls():
    model = scripted(
        ("list_pods", {"namespace": "catalog"}),
        ("get_service", {"namespace": "catalog", "name": "catalog"}),
        ("final", answer("tc_1", "tc_2")),
    )
    report = await run(model)
    assert report.status == "complete" and report.stop_reason is None
    assert report.diagnosis.root_cause.resource == "service/catalog"
    assert [c.id for c in report.tool_calls] == ["tc_1", "tc_2"]
    assert [c.tool for c in report.tool_calls] == ["list_pods", "get_service"]
    assert report.tool_calls[1].args == {"namespace": "catalog", "name": "catalog"}
    assert report.tool_calls[1].provider_id is not None
    assert Source(kind="Service", name="catalog", aspect="endpoints") in report.tool_calls[1].sources
    assert report.trace_id is None  # no tracer provider in this test


async def test_tool_output_starts_with_the_id_the_model_cites():
    seen = []

    def model(messages, info):
        for m in messages:
            seen.extend(p.content for p in m.parts if isinstance(p, ToolReturnPart))
        if not seen:
            return ModelResponse(parts=[ToolCallPart("get_service", {"namespace": "catalog", "name": "catalog"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer("tc_1"))])

    await run(FunctionModel(model))
    assert seen[0].startswith("[tc_1]\nService catalog:")


async def test_the_namespace_is_an_enum_of_the_allowed_namespaces():
    schemas = {}

    def model(messages, info):
        schemas.update({t.name: t.parameters_json_schema for t in info.function_tools})
        return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "kube-system"})])

    report = await run(FunctionModel(model), Settings(namespaces=("catalog", "shop"), request_limit=3))
    assert schemas["list_pods"]["properties"]["namespace"]["enum"] == ["catalog", "shop"]
    assert len(schemas) == 8  # seven reads, and apply_fix, which needs approval
    assert "apply_fix" in schemas
    # A namespace outside the enum never reaches the cluster: no call is recorded.
    assert report.tool_calls == []


async def test_citing_an_unknown_id_makes_the_model_retry():
    retries = []

    def model(messages, info):
        # The retry message reaches the model as the output tool's result.
        retries.extend(p.content for m in messages for p in m.parts if isinstance(p, (RetryPromptPart, ToolReturnPart)))
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        steps = [
            ToolCallPart("list_pods", {"namespace": "catalog"}),
            ToolCallPart(info.output_tools[0].name, answer("tc_1", "tc_9")),
            ToolCallPart(info.output_tools[0].name, answer("tc_1")),
        ]
        return ModelResponse(parts=[steps[n]])

    report = await run(FunctionModel(model))
    assert report.status == "complete"
    assert any("tc_9" in str(r) and "tc_1" in str(r) for r in retries)


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (Settings(namespaces=("catalog",), request_limit=3, tool_calls_limit=50), "request_limit"),
        (Settings(namespaces=("catalog",), request_limit=50, tool_calls_limit=2), "tool_call_limit"),
    ],
)
async def test_each_limit_stops_the_run_with_its_reason(settings, reason):
    report = await run(FunctionModel(loop_forever), settings)
    assert report.status == "stopped" and report.stop_reason == reason
    assert report.diagnosis is None
    assert report.tool_calls  # what happened before the stop is kept


async def test_a_slow_model_stops_with_timeout():
    async def slow(messages, info):
        await asyncio.sleep(5)

    report = await run(FunctionModel(slow), Settings(namespaces=("catalog",), run_timeout_s=0.1))
    assert report.stop_reason == "timeout"


async def test_an_answer_that_never_validates_stops_with_invalid_output():
    bad = answer("tc_1") | {"root_cause": {"category": "made_up", "resource": None, "explanation": "?"}}
    model = FunctionModel(lambda m, info: ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, bad)]))
    report = await run(model)
    assert report.stop_reason == "invalid_output"


async def test_a_model_error_stops_with_model_error():
    def broken(messages, info):
        raise RuntimeError("provider is down")

    report = await run(FunctionModel(broken))
    assert report.stop_reason == "model_error"


async def test_a_model_error_can_be_raised_instead_of_reported():
    # For callers that must tell "no answer" (a rate limit, an outage) from a wrong answer.
    def broken(messages, info):
        raise RuntimeError("429 rate limited")

    with pytest.raises(RuntimeError, match="429"):
        await run(FunctionModel(broken), raise_model_errors=True)


async def test_a_limit_is_still_reported_when_model_errors_are_raised():
    report = await run(FunctionModel(loop_forever), Settings(namespaces=("catalog",), tool_calls_limit=1), raise_model_errors=True)
    assert report.stop_reason == "tool_call_limit"


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = tracing.make_provider(SimpleSpanProcessor(exporter))
    previous = Agent._instrument_default
    tracing.instrument(provider)
    yield provider, exporter
    Agent.instrument_all(previous)


async def test_the_trace_has_a_root_span_whose_id_is_in_the_report(spans):
    provider, exporter = spans
    model = scripted(("list_pods", {"namespace": "catalog"}), ("final", answer("tc_1")))
    report = await run(model, tracer_provider=provider)
    finished = exporter.get_finished_spans()
    root = next(s for s in finished if s.name == "diagnose")
    assert format(root.context.trace_id, "032x") == report.trace_id
    assert root.parent is None
    assert root.attributes["input.value"] == QUESTION
    assert root.attributes["diagnoster.status"] == "complete"
    # The agent's own spans (run, model requests, tool calls) hang under it, in the same trace.
    children = [s for s in finished if s is not root]
    assert len(children) >= 3
    assert {s.context.trace_id for s in children} == {root.context.trace_id}
    assert any("list_pods" in s.name for s in children)


async def test_a_stopped_run_marks_the_root_span_as_an_error(spans):
    provider, exporter = spans
    await run(FunctionModel(loop_forever), Settings(namespaces=("catalog",), tool_calls_limit=1), tracer_provider=provider)
    root = next(s for s in exporter.get_finished_spans() if s.name == "diagnose")
    assert root.status.status_code.name == "ERROR"
    assert root.attributes["diagnoster.stop_reason"] == "tool_call_limit"


async def test_after_runs_inside_the_root_span_with_the_report(spans):
    provider, exporter = spans
    seen = []
    tracer = provider.get_tracer("test")

    def after(report):
        with tracer.start_as_current_span("remediate"):
            seen.append(report.status)

    model = scripted(("list_pods", {"namespace": "catalog"}), ("final", answer("tc_1")))
    await run(model, tracer_provider=provider, after=after)
    finished = {s.name: s for s in exporter.get_finished_spans()}
    assert seen == ["complete"]
    assert finished["remediate"].parent.span_id == finished["diagnose"].context.span_id


# ---- apply_fix: the one tool that changes something, only with approval ---------------

FIX = {"target": "deployment/payments-api", "field": "readinessProbe.httpGet.path", "from": "/healthz", "to": "/health"}


def remediate_answer(**fix_changes) -> dict:
    return {
        "root_cause": {"category": "bad_probe", "resource": "deployment/payments-api", "explanation": "Wrong probe path."},
        "evidence": [{"fact": "Pods are not ready", "tool_call_ids": ["tc_1"]}],
        "next_action": {"summary": "Fix the probe path.", "kind": "remediate", "proposed_fix": FIX | fix_changes},
    }


def asks_to_fix(seen: list, **fix_changes):
    """Reads once, asks to apply its fix, then answers. `seen` gets what apply_fix returned."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        if n == 0:
            return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "catalog"})])
        if n == 1:
            args = {"diagnosis": remediate_answer(**fix_changes), "expected_effect": "The pods become ready."}
            return ModelResponse(parts=[ToolCallPart("apply_fix", args, tool_call_id="fix_1")])
        seen.extend(p.content for p in messages[-1].parts if getattr(p, "tool_name", None) == "apply_fix")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, remediate_answer(**fix_changes))])

    return FunctionModel(model)


def never_fix(diagnosis):
    raise AssertionError("nothing may be applied")


async def test_where_no_one_can_approve_the_fix_is_denied_and_the_run_finishes():
    seen = []
    report = await run(asks_to_fix(seen), fixer=never_fix)
    assert report.status == "complete"
    assert report.remediation.status == "not_asked"
    assert report.remediation.fix.to == "/health"
    assert seen == ["No one can approve changes here, so nothing was changed. Give your final answer."]


async def test_a_fix_outside_the_allowlist_is_refused_before_anyone_is_asked():
    seen, paused = [], []
    report = await run(asks_to_fix(seen, target="deployment/checkout"), no_approver=None, on_pause=paused.append, fixer=never_fix)
    assert report.status == "complete" and paused == []
    assert report.remediation.status == "not_allowed"
    assert "only deployment/payments-api may be changed" in seen[0]


async def test_with_a_person_the_run_pauses_then_applies_the_approved_fix():
    seen, paused, applied = [], [], []

    def fixer(diagnosis):
        applied.append(diagnosis.next_action.proposed_fix.to)
        return Remediation(status="fixed", detail="patched; pods ready 2/2", fix=diagnosis.next_action.proposed_fix)

    report = await run(asks_to_fix(seen), no_approver=None, on_pause=paused.append, fixer=fixer)
    assert report.status == "awaiting_approval"
    assert report.remediation.status == "asked"
    assert report.remediation.expected_effect == "The pods become ready."
    assert report.diagnosis.next_action.proposed_fix.to == "/health"
    assert applied == []  # nothing happens before the answer
    [waiting] = paused

    final = await resume(waiting, approved=True)
    assert applied == ["/health"]
    assert final.status == "complete"
    assert final.remediation.status == "fixed"
    assert final.remediation.expected_effect == "The pods become ready."
    assert seen == ["fixed: patched; pods ready 2/2"]
    assert [c.id for c in final.tool_calls] == ["tc_1"]  # the record carries over


async def test_a_denied_fix_changes_nothing_and_the_agent_says_so():
    seen, paused = [], []
    await run(asks_to_fix(seen), no_approver=None, on_pause=paused.append, fixer=never_fix)
    final = await resume(paused[0], approved=False)
    assert final.status == "complete"
    assert final.remediation.status == "denied"
    assert seen == ["The user denied the fix. Nothing was changed. Give your final answer."]


async def test_a_remediate_answer_must_offer_the_fix_first():
    """The model answers remediate without asking; our rule sends it back to apply_fix."""
    told = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        if n == 0:
            return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "catalog"})])
        if n == 1:  # skips apply_fix
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, remediate_answer())])
        if n == 2:
            told.extend(str(p.content) for p in messages[-1].parts)
            args = {"diagnosis": remediate_answer(), "expected_effect": "The pods become ready."}
            return ModelResponse(parts=[ToolCallPart("apply_fix", args, tool_call_id="fix_1")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, remediate_answer())])

    report = await run(FunctionModel(model), fixer=never_fix)
    assert "call apply_fix with this diagnosis" in told[0]
    assert report.status == "complete" and report.remediation.status == "not_asked"


async def test_a_run_that_uses_too_many_tokens_stops_with_token_limit():
    report = await run(FunctionModel(loop_forever), Settings(namespaces=("catalog",), total_tokens_limit=300))
    assert report.status == "stopped" and report.stop_reason == "token_limit"
