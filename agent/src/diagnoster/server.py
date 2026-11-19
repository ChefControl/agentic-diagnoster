"""The agent as an HTTP service that speaks both OpenAI's and Anthropic's chat formats.

Phoenix can then call the agent like any chat model: it is registered there
as a custom AI provider once per format, so the Playground and its
experiments run the real agent, with the model you pick in Phoenix's menu.

    GET  /healthz
    GET  /v1/models
    POST /v1/chat/completions   OpenAI's format; a plain model name is an OpenAI model
    POST /v1/messages           Anthropic's format; a plain model name is a Claude model
    POST /v1/runs               for people (the UI's chat): the report as JSON, with a session id

The cluster is the agent's own setting, CLUSTER_MODE: `live` reads the real
cluster as the diagnoster-reader; `recorded` replays the saved snapshot and
needs no cluster. The reply says which one answered.

From the request:
- the question is the last user message;
- a system prompt, when there is one, replaces the agent's prompt for this
  run, so a prompt edited in the Playground is the prompt that runs;
- the model: `provider:model` as given; a plain name belongs to the format's
  vendor; none (or "default") means the agent's default MODEL.

The answer is one assistant message (see reply.py). A run that hits a limit
still answers 200 with a stopped report: that is a result. A model failure
(a rate limit, an outage, a missing key, an unknown model) answers 502, so it
is recorded as an error and not as a wrong answer.
"""

import dataclasses
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cache
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai.models import Model
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import recorded
from .agent import diagnose
from .cluster import Cluster, connect
from .config import Settings, load_settings
from .reply import render
from .tracing import setup

log = logging.getLogger(__name__)

# "openai:gpt-6-luna": a provider PydanticAI knows, then the model's name there.
QUALIFIED = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class BadRequest(Exception):
    pass


@dataclass(frozen=True)
class Ask:
    """What a request asks, whatever its format."""

    question: str
    system_prompt: str | None
    model: str | None
    stream: bool


def _text(content: Any) -> str:
    """A message's text: a plain string, or a list of parts with text in them."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type", "text") == "text")
    return ""


def _ask(body: Any, system: list[str]) -> Ask:
    try:
        messages = [m for m in body["messages"] if isinstance(m, dict)]
    except (KeyError, TypeError):
        raise BadRequest("The body must be JSON with a `messages` list.") from None
    questions = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
    if not questions or not questions[-1].strip():
        raise BadRequest("No user message: the last user message is the question.")
    prompt = "\n\n".join(p for p in system if p.strip()) or None
    model = body.get("model") if isinstance(body.get("model"), str) else None
    return Ask(questions[-1], prompt, model, bool(body.get("stream")))


def openai_ask(body: Any) -> Ask:
    """OpenAI's format: the prompt is in system (or developer) messages."""
    messages = body.get("messages") if isinstance(body, dict) else None
    system = [_text(m.get("content")) for m in messages or [] if isinstance(m, dict) and m.get("role") in ("system", "developer")]
    return _ask(body, system)


def anthropic_ask(body: Any) -> Ask:
    """Anthropic's format: the prompt is the top-level `system`, a string or text blocks."""
    system = _text(body.get("system")) if isinstance(body, dict) else ""
    return _ask(body, [system])


def model_for(requested: str | None, vendor: str, default: str) -> str:
    if not requested or requested == "default":
        return default
    if QUALIFIED.match(requested):
        return requested
    return f"{vendor}:{requested}"


# ---- the two wire formats ----------------------------------------------------------


def _openai_error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def _anthropic_error(status: int, message: str, kind: str) -> JSONResponse:
    kinds = {"invalid_request_error": "invalid_request_error", "not_found": "not_found_error"}
    return JSONResponse({"type": "error", "error": {"type": kinds.get(kind, "api_error"), "message": message}}, status_code=status)


def _openai_reply(model: str, content: str, stream: bool) -> Response:
    base = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "created": int(time.time()), "model": model}
    if not stream:
        choice = {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        return JSONResponse(base | {"object": "chat.completion", "choices": [choice]})

    def chunks() -> Iterator[str]:
        # Who speaks, the text, the end. Final answer only, no live progress.
        for delta, finish in (({"role": "assistant", "content": ""}, None), ({"content": content}, None), ({}, "stop")):
            chunk = base | {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


def _anthropic_reply(model: str, content: str, stream: bool) -> Response:
    # Token usage stays in the agent's own trace; the caller sees zeros.
    message = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if not stream:
        return JSONResponse(message)

    def events() -> Iterator[str]:
        sequence = [
            ("message_start", {"message": message | {"content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": content}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}}),
            ("message_stop", {}),
        ]
        for name, data in sequence:
            yield f"event: {name}\ndata: {json.dumps({'type': name} | data)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


@dataclass(frozen=True)
class Format:
    vendor: str  # who a plain model name belongs to
    ask: Callable[[Any], Ask]
    reply: Callable[[str, str, bool], Response]
    error: Callable[[int, str, str], JSONResponse]


OPENAI = Format("openai", openai_ask, _openai_reply, _openai_error)
ANTHROPIC = Format("anthropic", anthropic_ask, _anthropic_reply, _anthropic_error)


# ---- the app ------------------------------------------------------------------------


def create_app(
    settings: Settings,
    *,
    resolve_model: Callable[[str], Model | str] = lambda name: name,
    live: Callable[[], Cluster] | None = None,
    load_recorded: Callable[[], recorded.Snapshot] = cache(recorded.load),
    tracer_provider: TracerProvider | None = None,
) -> Starlette:
    """The app. Tests pass a scripted model and fake clusters; `app_from_env` passes nothing."""
    # A fresh connection per request picks up a re-issued token.
    live = live or (lambda: connect(settings.reader_kubeconfig))

    def cluster() -> tuple[Cluster, Settings]:
        if settings.cluster_mode == "live":
            return live(), settings
        snapshot = load_recorded()
        # The recorded cluster is its own world: the tools offer its namespaces.
        return snapshot.cluster, dataclasses.replace(settings, namespaces=snapshot.namespaces)

    async def answer(request: Request, fmt: Format) -> Response:
        try:
            ask = fmt.ask(await request.json())
        except ValueError:
            return fmt.error(400, "The body must be JSON.", "invalid_request_error")
        except BadRequest as e:
            return fmt.error(400, str(e), "invalid_request_error")
        model_name = model_for(ask.model, fmt.vendor, settings.model)
        try:
            run_cluster, run_settings = cluster()
        except FileNotFoundError as e:  # no kubeconfig (live) or no snapshot (recorded)
            return fmt.error(503, str(e), "cluster_unavailable")
        try:
            report = await diagnose(
                ask.question,
                run_settings,
                run_cluster,
                model=resolve_model(model_name),
                system_prompt=ask.system_prompt,
                attributes={
                    "diagnoster.cluster": settings.cluster_mode,
                    "diagnoster.prompt_source": "request" if ask.system_prompt else "code",
                },
                tracer_provider=tracer_provider,
                raise_model_errors=True,
            )
        except Exception as e:  # noqa: BLE001 - whatever stopped the model, the caller gets an error, not an answer
            log.warning("model error with %s: %s: %s", model_name, type(e).__name__, e)
            return fmt.error(502, f"{type(e).__name__}: {e}", "model_error")
        content = render(report, model_name, settings.phoenix_url, cluster=settings.cluster_mode)
        return fmt.reply(model_name, content, ask.stream)

    async def runs(request: Request) -> Response:
        """One chat question. The UI draws the answer from `report`.

        `session_id` groups a chat's runs into one Phoenix session. Model
        failures answer 502, like the vendor formats, so the chat can say what
        went wrong instead of showing an empty answer.
        """
        try:
            body = await request.json()
        except ValueError:
            return _openai_error(400, "The body must be JSON.", "invalid_request_error")
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or not question.strip():
            return _openai_error(400, "The body needs a `question`.", "invalid_request_error")
        session_id = body.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.match(session_id)):
            return _openai_error(400, "`session_id` must be 1-128 letters, digits, - or _.", "invalid_request_error")
        requested = body.get("model") if isinstance(body.get("model"), str) else None
        model_name = model_for(requested, "openai", settings.model)
        try:
            run_cluster, run_settings = cluster()
        except FileNotFoundError as e:
            return _openai_error(503, str(e), "cluster_unavailable")
        attributes = {"diagnoster.cluster": settings.cluster_mode, "diagnoster.prompt_source": "code", "diagnoster.entry": "chat"}
        try:
            report = await diagnose(
                question.strip(),
                run_settings,
                run_cluster,
                model=resolve_model(model_name),
                attributes=attributes,
                session_id=session_id,
                tracer_provider=tracer_provider,
                raise_model_errors=True,
            )
        except Exception as e:  # noqa: BLE001 - the chat gets an error, not an answer
            log.warning("model error with %s: %s: %s", model_name, type(e).__name__, e)
            return _openai_error(502, f"{type(e).__name__}: {e}", "model_error")
        trace_url = f"{settings.phoenix_url}/redirects/traces/{report.trace_id}" if report.trace_id else None
        return JSONResponse(
            {
                "run_id": uuid.uuid4().hex,
                "status": report.status,
                "model": model_name,
                "cluster": settings.cluster_mode,
                "trace_url": trace_url,
                "report": report.model_dump(mode="json"),
            }
        )

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "cluster": settings.cluster_mode, "model": settings.model})

    async def models(request: Request) -> Response:
        return JSONResponse({"object": "list", "data": [{"id": settings.model, "object": "model", "owned_by": "diagnoster"}]})

    async def chat_completions(request: Request) -> Response:
        return await answer(request, OPENAI)

    async def messages(request: Request) -> Response:
        return await answer(request, ANTHROPIC)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        if tracer_provider is not None:
            tracer_provider.force_flush()

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/v1/models", models),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/messages", messages, methods=["POST"]),
            Route("/v1/runs", runs, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def app_from_env() -> Starlette:
    """What uvicorn serves: `uvicorn diagnoster.server:app_from_env --factory`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    log.info("cluster: %s, default model: %s", settings.cluster_mode, settings.model)
    return create_app(settings, tracer_provider=setup(settings.phoenix_collector_endpoint))
