"""The HTTP service with a scripted model: no network, no API key, no cluster."""

import json

import socket
import threading
import time

import anthropic
import openai
import pytest
import uvicorn
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
from diagnoster.server import create_app, model_for

SETTINGS = Settings(model="openai:the-default", namespaces=("catalog", "shop"), phoenix_url="http://phoenix.test")
ASK = {"messages": [{"role": "user", "content": QUESTION}]}
CLAUDE_ASK = {"model": "claude-haiku-5-5", "max_tokens": 1024, "messages": [{"role": "user", "content": QUESTION}]}


def diagnosing():
    """A model that looks at the catalog Service and answers."""
    return scripted(("get_service", {"namespace": "catalog", "name": "catalog"}), ("final", answer("tc_1")))


def client(model=None, settings=SETTINGS, **kw) -> TestClient:
    kw.setdefault("live", lambda: selector_mismatch_world().cluster())
    return TestClient(create_app(settings, resolve_model=lambda name: model or diagnosing(), **kw))


def openai_content(response) -> str:
    assert response.status_code == 200, response.text
    return response.json()["choices"][0]["message"]["content"]


def report_in(content: str) -> RunReport:
    """The reply is JSON: the report plus the model, the cluster and the trace link."""
    reply = json.loads(content)
    return RunReport.model_validate({k: v for k, v in reply.items() if k not in ("model", "cluster", "trace_url")})


def test_the_openai_format_answers_with_a_summary_and_the_full_report():
    content = openai_content(client().post("/v1/chat/completions", json=ASK))
    assert json.loads(content)["cluster"] == "live"
    assert report_in(content).diagnosis.root_cause.resource == "service/catalog"
    report = report_in(content)
    assert report.status == "complete"
    assert [c.tool for c in report.tool_calls] == ["get_service"]


def test_the_anthropic_format_gives_the_same_answer_as_a_claude_message():
    response = client().post("/v1/messages", json=CLAUDE_ASK)
    assert response.status_code == 200, response.text
    message = response.json()
    assert (message["type"], message["role"], message["stop_reason"]) == ("message", "assistant", "end_turn")
    content = message["content"][0]["text"]
    assert report_in(content).diagnosis.root_cause.category == "selector_mismatch"


def test_recorded_mode_answers_from_the_snapshot_with_no_cluster():
    def no_cluster():
        raise AssertionError("recorded mode must not connect to a cluster")

    # Settings without the catalog namespace: the snapshot brings its own.
    settings = Settings(model="openai:the-default", namespaces=("elsewhere",), cluster_mode="recorded")
    content = openai_content(client(settings=settings, live=no_cluster).post("/v1/chat/completions", json=ASK))
    assert json.loads(content)["cluster"] == "recorded"
    report = report_in(content)
    assert report.status == "complete"
    assert report.tool_calls[0].sources  # the Service was found in the snapshot


@pytest.mark.parametrize(
    ("path", "with_prompt"),
    [
        ("/v1/chat/completions", {"messages": [{"role": "system", "content": "Answer in one tool call."}, *ASK["messages"]]}),
        ("/v1/messages", CLAUDE_ASK | {"system": "Answer in one tool call."}),
        ("/v1/messages", CLAUDE_ASK | {"system": [{"type": "text", "text": "Answer in one tool call."}]}),
    ],
)
def test_a_system_prompt_is_the_prompt_for_that_run(path, with_prompt):
    seen = []

    def model(messages, info):
        seen.extend(p.content for m in messages for p in m.parts if isinstance(p, SystemPromptPart))
        return ModelResponse(parts=[ToolCallPart("list_pods", {"namespace": "catalog"})])

    app = client(FunctionModel(model), Settings(namespaces=("catalog",), request_limit=1))
    app.post(path, json={k: v for k, v in with_prompt.items() if k != "system"} if path == "/v1/messages" else ASK)
    assert seen == [SYSTEM_PROMPT]
    seen.clear()
    app.post(path, json=with_prompt)
    assert seen == ["Answer in one tool call."]


@pytest.mark.parametrize(
    ("requested", "vendor", "used"),
    [
        ("gpt-6.1-sol", "openai", "openai:gpt-6.1-sol"),  # a name from Phoenix's OpenAI menu
        ("claude-haiku-5-5", "anthropic", "anthropic:claude-haiku-5-5"),  # from its Claude menu
        ("anthropic:claude-opus-5-5", "openai", "anthropic:claude-opus-5-5"),  # provider:model is used as given
        ("default", "anthropic", "openai:the-default"),
        (None, "openai", "openai:the-default"),
        ("", "anthropic", "openai:the-default"),
    ],
)
def test_the_model_is_the_requested_one_or_the_default(requested, vendor, used):
    assert model_for(requested, vendor, "openai:the-default") == used


def test_the_reply_names_the_model_that_answered():
    asked = []

    def resolve(name):
        asked.append(name)
        return diagnosing()

    app = TestClient(create_app(SETTINGS, resolve_model=resolve, live=lambda: selector_mismatch_world().cluster()))
    response = app.post("/v1/messages", json=CLAUDE_ASK)
    assert asked == ["anthropic:claude-haiku-5-5"]
    assert response.json()["model"] == "anthropic:claude-haiku-5-5"
    assert json.loads(response.json()["content"][0]["text"])["model"] == "anthropic:claude-haiku-5-5"


def test_an_openai_stream_carries_the_same_answer_in_openai_chunks():
    app = client()
    plain = openai_content(app.post("/v1/chat/completions", json=ASK))
    response = app.post("/v1/chat/completions", json=ASK | {"stream": True, "stream_options": {"include_usage": True}})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line.removeprefix("data: ") for line in response.text.splitlines() if line]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert "".join(d.get("content", "") for d in deltas) == plain
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, "stop"]


# The vendors' own SDKs read our replies, as Phoenix does.


@pytest.fixture
def served():
    """The app on a real local port, the way Phoenix reaches it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(SETTINGS, resolve_model=lambda name: diagnosing(), live=lambda: selector_mismatch_world().cluster())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_the_openai_sdk_reads_both_shapes(served):
    sdk = openai.OpenAI(api_key="not-needed", base_url=f"{served}/v1", max_retries=0)
    plain = sdk.chat.completions.create(model="gpt-6-luna", messages=ASK["messages"])
    streamed = "".join(c.choices[0].delta.content or "" for c in sdk.chat.completions.create(model="gpt-6-luna", messages=ASK["messages"], stream=True))
    assert plain.choices[0].message.content == streamed
    assert report_in(streamed).status == "complete"


def test_the_anthropic_sdk_reads_both_shapes(served):
    sdk = anthropic.Anthropic(api_key="not-needed", base_url=served, max_retries=0)
    plain = sdk.messages.create(**CLAUDE_ASK)
    with sdk.messages.stream(**CLAUDE_ASK) as stream:
        streamed = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert plain.content[0].text == streamed
    assert final.stop_reason == "end_turn"
    assert report_in(streamed).status == "complete"


@pytest.mark.parametrize(("path", "body"), [("/v1/chat/completions", ASK), ("/v1/messages", CLAUDE_ASK)])
def test_a_model_error_is_a_502_not_an_answer(path, body):
    def broken(messages, info):
        raise RuntimeError("429 rate limited")

    response = client(FunctionModel(broken)).post(path, json=body)
    assert response.status_code == 502
    assert "RuntimeError: 429 rate limited" in response.text


def test_the_anthropic_error_shape():
    def broken(messages, info):
        raise RuntimeError("overloaded")

    response = client(FunctionModel(broken)).post("/v1/messages", json=CLAUDE_ASK)
    assert response.json() == {"type": "error", "error": {"type": "api_error", "message": "RuntimeError: overloaded"}}


def test_a_run_that_hits_a_limit_is_still_an_answer():
    settings = Settings(namespaces=("catalog",), tool_calls_limit=1)
    content = openai_content(client(FunctionModel(loop_forever), settings).post("/v1/chat/completions", json=ASK))
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
    app = client(FunctionModel(model), Settings(namespaces=("catalog",), request_limit=1))
    app.post("/v1/chat/completions", json=body)
    app.post("/v1/messages", json=body | {"max_tokens": 10})
    assert asked == [QUESTION, QUESTION]


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages"])
@pytest.mark.parametrize(
    "body",
    [{}, {"messages": []}, {"messages": [{"role": "system", "content": "Only a prompt."}]}, {"messages": [{"role": "user", "content": " "}]}],
)
def test_a_request_without_a_question_is_a_400(path, body):
    response = client().post(path, json=body)
    assert response.status_code == 400
    assert "invalid_request_error" in response.text


def test_live_mode_without_a_kubeconfig_is_a_503_that_says_what_to_run(tmp_path):
    settings = Settings(reader_kubeconfig=tmp_path / "missing.kubeconfig")
    app = TestClient(create_app(settings, resolve_model=lambda name: diagnosing()))
    response = app.post("/v1/chat/completions", json=ASK)
    assert response.status_code == 503
    assert "./cluster.sh --up" in response.json()["error"]["message"]


def test_health_says_the_cluster_and_the_default_model():
    app = client()
    assert app.get("/healthz").json() == {"status": "ok", "cluster": "live", "model": "openai:the-default"}
    assert [m["id"] for m in app.get("/v1/models").json()["data"]] == ["openai:the-default"]


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = tracing.make_provider(SimpleSpanProcessor(exporter))
    previous = Agent._instrument_default
    tracing.instrument(provider)
    yield provider, exporter
    Agent.instrument_all(previous)


def test_the_root_span_says_the_cluster_and_where_the_prompt_came_from(spans):
    provider, exporter = spans
    settings = dataclasses_replace(SETTINGS, cluster_mode="recorded")
    app = client(settings=settings, tracer_provider=provider)
    tried = {"messages": [{"role": "system", "content": "A prompt being tried."}, *ASK["messages"]]}
    content = openai_content(app.post("/v1/chat/completions", json=tried))
    root = next(s for s in exporter.get_finished_spans() if s.name == "diagnose")
    assert root.attributes["diagnoster.cluster"] == "recorded"
    assert root.attributes["diagnoster.prompt_source"] == "request"
    # The reply links to this trace: Phoenix sends no trace context, so this is the only link.
    assert json.loads(content)["trace_url"] == f"http://phoenix.test/redirects/traces/{format(root.context.trace_id, '032x')}"


def dataclasses_replace(settings, **changes):
    import dataclasses

    return dataclasses.replace(settings, **changes)


# ---- /v1/runs: the chat's endpoint ---------------------------------------------------


def test_a_chat_run_answers_with_the_report_and_where_it_came_from():
    reply = client().post("/v1/runs", json={"question": QUESTION, "session_id": "chat-1"}).json()
    assert reply["status"] == "complete"
    assert reply["model"] == "openai:the-default"
    assert reply["cluster"] == "live"
    assert len(reply["run_id"]) == 32
    report = RunReport.model_validate(reply["report"])
    assert report.diagnosis.root_cause.resource == "service/catalog"


def test_a_chat_run_that_hits_a_limit_is_stopped_not_an_error():
    settings = Settings(namespaces=("catalog",), tool_calls_limit=1)
    reply = client(FunctionModel(loop_forever), settings).post("/v1/runs", json={"question": QUESTION}).json()
    assert reply["status"] == "stopped"
    assert reply["report"]["stop_reason"] == "tool_call_limit"


@pytest.mark.parametrize(
    "body",
    [{}, {"question": " "}, {"question": 3}, {"question": QUESTION, "session_id": "has space"}, {"question": QUESTION, "session_id": 7}],
)
def test_a_chat_run_needs_a_question_and_a_plain_session_id(body):
    assert client().post("/v1/runs", json=body).status_code == 400


def test_a_chat_run_with_a_model_error_is_a_502():
    def broken(messages, info):
        raise RuntimeError("429 rate limited")

    response = client(FunctionModel(broken)).post("/v1/runs", json={"question": QUESTION})
    assert response.status_code == 502
    assert "429 rate limited" in response.json()["error"]["message"]


def test_a_chat_run_carries_its_session_into_the_trace(spans):
    provider, exporter = spans
    reply = client(tracer_provider=provider).post("/v1/runs", json={"question": QUESTION, "session_id": "chat-42"}).json()
    root = next(s for s in exporter.get_finished_spans() if s.name == "diagnose")
    assert root.attributes["session.id"] == "chat-42"
    # PydanticAI's spans carry the same id, so Phoenix sees one session, not one per run.
    run = next(s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent"))
    assert run.attributes["gen_ai.conversation.id"] == "chat-42"
    assert root.attributes["diagnoster.entry"] == "chat"
    assert reply["trace_url"].endswith(format(root.context.trace_id, "032x"))


# ---- fixes: paused in the chat, denied everywhere else ---------------------------------

from test_agent import asks_to_fix  # noqa: E402
from test_remediation import payments  # noqa: E402

ASK_RUN = {"question": QUESTION, "session_id": "chat-7"}


def fixing_app(seen, world=None, clock=lambda: 0.0, settings=SETTINGS, **kw):
    world = world or payments()
    return world, TestClient(
        create_app(
            settings,
            resolve_model=lambda name: asks_to_fix(seen),
            live=lambda: selector_mismatch_world().cluster(),
            remediator=world.cluster,
            verify_fn=lambda reader: (True, ["pods ready 2/2"]),
            clock=clock,
            **kw,
        )
    )


def test_the_chat_waits_for_approval_then_the_agent_applies_the_fix():
    seen = []
    world, app = fixing_app(seen)
    asked = app.post("/v1/runs", json=ASK_RUN).json()
    assert asked["status"] == "awaiting_approval"
    assert asked["report"]["diagnosis"]["next_action"]["proposed_fix"]["to"] == "/health"
    assert world.patches == []

    done = app.post(f"/v1/runs/{asked['run_id']}/approval", json={"approved": True}).json()
    assert done["status"] == "complete" and done["run_id"] == asked["run_id"]
    assert done["report"]["remediation"]["status"] == "fixed"
    assert len(world.patches) == 1
    # One answer per run: a second click finds nothing to approve.
    again = app.post(f"/v1/runs/{asked['run_id']}/approval", json={"approved": True})
    assert again.status_code == 404 and len(world.patches) == 1


def test_a_denied_fix_changes_nothing():
    seen = []
    world, app = fixing_app(seen)
    run_id = app.post("/v1/runs", json=ASK_RUN).json()["run_id"]
    done = app.post(f"/v1/runs/{run_id}/approval", json={"approved": False}).json()
    assert done["report"]["remediation"]["status"] == "denied"
    assert world.patches == []


def test_a_waiting_fix_expires_after_30_minutes():
    now = [0.0]
    _, app = fixing_app([], clock=lambda: now[0])
    run_id = app.post("/v1/runs", json=ASK_RUN).json()["run_id"]
    now[0] = 30 * 60 + 1
    response = app.post(f"/v1/runs/{run_id}/approval", json={"approved": True})
    assert response.status_code == 404 and "expired" in response.json()["error"]["message"]


@pytest.mark.parametrize("body", [{}, {"approved": "yes"}, None])
def test_an_approval_needs_true_or_false(body):
    _, app = fixing_app([])
    assert app.post("/v1/runs/" + "a" * 32 + "/approval", json=body).status_code == 400


def test_phoenix_and_evals_never_wait_the_fix_is_denied():
    seen = []
    world, app = fixing_app(seen)
    report = report_in(openai_content(app.post("/v1/chat/completions", json=ASK)))
    assert report.status == "complete" and report.remediation.status == "not_asked"
    assert seen == ["No one can approve changes here, so nothing was changed. Give your final answer."]
    assert world.patches == []


def test_the_recorded_cluster_never_offers_the_fix():
    seen = []
    world, app = fixing_app(seen, settings=dataclasses_replace(SETTINGS, cluster_mode="recorded"))
    reply = app.post("/v1/runs", json=ASK_RUN).json()
    assert reply["status"] == "complete" and reply["report"]["remediation"]["status"] == "not_asked"
    assert seen == ["The recorded cluster can't be changed, so nothing was changed. Give your final answer."]
    assert world.patches == []
