"""The HTTP service with a scripted model: no network, no API key, no cluster."""

import json

import pytest
from fakes import selector_mismatch_world
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, SystemPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from starlette.testclient import TestClient
from test_agent import QUESTION, answer, loop_forever, scripted

from diagnoster import tracing
from diagnoster.agent import SYSTEM_PROMPT
from diagnoster.config import Settings
from diagnoster.contract import RunReport
from diagnoster.server import create_app

SETTINGS = Settings(model="test:default", namespaces=("catalog", "shop"), phoenix_url="http://phoenix.test")
ASK = {"messages": [{"role": "user", "content": QUESTION}]}


def diagnosing():
    """A model that looks at the catalog Service and answers."""
    return scripted(("get_service", {"namespace": "catalog", "name": "catalog"}), ("final", answer("tc_1")))


def client(model=None, settings=SETTINGS, **kw) -> TestClient:
    kw.setdefault("live", lambda: selector_mismatch_world().cluster())
    return TestClient(create_app(settings, resolve_model=lambda name: model or diagnosing(), **kw))


def content_of(response) -> str:
    assert response.status_code == 200, response.text
    return response.json()["choices"][0]["message"]["content"]


def report_in(content: str) -> RunReport:
    """The reply's last fenced json block is the full report."""
    block = content[content.rindex("```json\n") + len("```json\n") : content.rindex("\n```")]
    return RunReport.model_validate_json(block)


def test_live_mode_answers_with_a_summary_and_the_full_report():
    content = content_of(client().post("/live/v1/chat/completions", json=ASK))
    assert content.startswith("**Root cause**: selector_mismatch in service/catalog")
    report = report_in(content)
    assert report.status == "complete"
    assert [c.tool for c in report.tool_calls] == ["get_service"]


def test_recorded_mode_answers_from_the_snapshot_with_no_cluster():
    def no_cluster():
        raise AssertionError("recorded mode must not connect to a cluster")

    # Settings without the catalog namespace: the snapshot brings its own.
    settings = Settings(model="test:default", namespaces=("elsewhere",))
    content = content_of(client(settings=settings, live=no_cluster).post("/recorded/v1/chat/completions", json=ASK))
    report = report_in(content)
    assert report.status == "complete"
    assert report.tool_calls[0].sources  # the Service was found in the snapshot


def test_a_system_message_is_the_prompt_for_that_run():
    seen = []

    def model(messages, info):
        seen.extend(p.content for m in messages for p in m.parts if isinstance(p, SystemPromptPart))
        return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "catalog"})])

    settings = Settings(namespaces=("catalog",), request_limit=1)
    app = client(FunctionModel(model), settings)
    app.post("/live/v1/chat/completions", json=ASK)
    assert seen == [SYSTEM_PROMPT]

    seen.clear()
    tried = {"messages": [{"role": "system", "content": "Answer in one tool call."}, *ASK["messages"]]}
    app.post("/live/v1/chat/completions", json=tried)
    assert seen == ["Answer in one tool call."]


@pytest.mark.parametrize(
    ("requested", "used"),
    [
        ("openrouter:anthropic/claude-haiku-5.5", "openrouter:anthropic/claude-haiku-5.5"),
        ("diagnoster", "test:default"),  # any name that isn't provider:model means the default
        (None, "test:default"),
    ],
)
def test_the_model_is_the_requested_one_or_the_default(requested, used):
    asked = []

    def resolve(name):
        asked.append(name)
        return diagnosing()

    app = TestClient(create_app(SETTINGS, resolve_model=resolve, live=lambda: selector_mismatch_world().cluster()))
    response = app.post("/live/v1/chat/completions", json=ASK | ({"model": requested} if requested else {}))
    assert asked == [used]
    assert response.json()["model"] == used
    assert f"\nModel: {used}\n" in content_of(response)


def test_a_stream_carries_the_same_answer_in_openai_chunks():
    app = client()
    plain = content_of(app.post("/live/v1/chat/completions", json=ASK))
    response = app.post("/live/v1/chat/completions", json=ASK | {"stream": True, "stream_options": {"include_usage": True}})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line.removeprefix("data: ") for line in response.text.splitlines() if line]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert {c["object"] for c in chunks} == {"chat.completion.chunk"}
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert "".join(d.get("content", "") for d in deltas) == plain
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, "stop"]


def test_a_model_error_is_a_502_not_an_answer():
    def broken(messages, info):
        raise RuntimeError("429 rate limited")

    response = client(FunctionModel(broken)).post("/live/v1/chat/completions", json=ASK)
    assert response.status_code == 502
    assert response.json()["error"] == {"message": "RuntimeError: 429 rate limited", "type": "model_error"}


def test_a_run_that_hits_a_limit_is_still_an_answer():
    settings = Settings(namespaces=("catalog",), tool_calls_limit=1)
    content = content_of(client(FunctionModel(loop_forever), settings).post("/live/v1/chat/completions", json=ASK))
    assert content.startswith("**Stopped**: tool_call_limit after 1 tool calls.")
    assert report_in(content).stop_reason == "tool_call_limit"


def test_text_can_arrive_as_content_parts_and_the_last_user_message_is_the_question():
    asked = []

    def model(messages, info):
        asked.append(messages[0].parts[-1].content)
        return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "catalog"})])

    body = {
        "messages": [
            {"role": "user", "content": "An earlier question."},
            {"role": "assistant", "content": "An earlier answer."},
            {"role": "user", "content": [{"type": "text", "text": QUESTION}]},
        ]
    }
    client(FunctionModel(model), Settings(namespaces=("catalog",), request_limit=1)).post("/live/v1/chat/completions", json=body)
    assert asked == [QUESTION]


@pytest.mark.parametrize(
    "body",
    [{}, {"messages": []}, {"messages": [{"role": "system", "content": "Only a prompt."}]}, {"messages": [{"role": "user", "content": " "}]}],
)
def test_a_request_without_a_question_is_a_400(body):
    response = client().post("/live/v1/chat/completions", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_live_mode_without_a_kubeconfig_is_a_503_that_says_what_to_run(tmp_path):
    settings = Settings(reader_kubeconfig=tmp_path / "missing.kubeconfig")
    app = TestClient(create_app(settings, resolve_model=lambda name: diagnosing()))
    response = app.post("/live/v1/chat/completions", json=ASK)
    assert response.status_code == 503
    assert "./cluster.sh --up" in response.json()["error"]["message"]


def test_an_unknown_mode_is_a_404():
    assert client().post("/replay/v1/chat/completions", json=ASK).status_code == 404
    assert client().get("/replay/v1/models").status_code == 404


def test_health_and_the_model_list():
    app = client()
    assert app.get("/healthz").json() == {"status": "ok"}
    assert [m["id"] for m in app.get("/recorded/v1/models").json()["data"]] == ["test:default"]


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = tracing.make_provider(SimpleSpanProcessor(exporter))
    previous = Agent._instrument_default
    tracing.instrument(provider)
    yield provider, exporter
    Agent.instrument_all(previous)


def test_the_root_span_says_the_mode_and_where_the_prompt_came_from(spans):
    provider, exporter = spans
    app = client(tracer_provider=provider)
    tried = {"messages": [{"role": "system", "content": "A prompt being tried."}, *ASK["messages"]]}
    content = content_of(app.post("/recorded/v1/chat/completions", json=tried))
    root = next(s for s in exporter.get_finished_spans() if s.name == "diagnose")
    assert root.attributes["diagnoster.mode"] == "recorded"
    assert root.attributes["diagnoster.prompt_source"] == "request"
    assert root.attributes["diagnoster.model"] != ""
    # The reply links to this trace: Phoenix sends no trace context, so this is the only link.
    assert f"Trace: http://phoenix.test/redirects/traces/{format(root.context.trace_id, '032x')}" in content
