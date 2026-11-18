"""The agent as an HTTP service that speaks OpenAI's chat-completions format.

Phoenix can then call the agent like any chat model: it is registered there as
a custom AI provider, so the Playground and its experiments run the real agent.

    GET  /healthz
    GET  /{mode}/v1/models
    POST /{mode}/v1/chat/completions

`mode` picks the cluster: `live` reads the real one as the diagnoster-reader;
`recorded` replays the saved snapshot and needs no cluster.

From the request:
- the question is the last user message;
- a system message, when there is one, replaces the agent's prompt for this
  run, so a prompt edited in the Playground is the prompt that runs;
- `model` is used when it looks like `provider:model`; any other name means
  the default MODEL.

The answer is one assistant message (see reply.py). A run that hits a limit
still answers 200 with a stopped report: that is a result. A model failure
(a rate limit, an outage, a missing key) answers 502, so it is recorded as an
error and not as a wrong answer.
"""

import dataclasses
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
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

MODES = ("live", "recorded")
# "openrouter:openai/gpt-6-luna": a provider PydanticAI knows, then the model's id there.
MODEL_NAME = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def _text(content: Any) -> str:
    """A message's text: a plain string, or a list of parts with text in them."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _chunks(base: dict[str, Any], content: str):
    """The answer as a stream: who speaks, the text, the end. Final answer only, no live progress."""
    for delta, finish in (({"role": "assistant", "content": ""}, None), ({"content": content}, None), ({}, "stop")):
        chunk = base | {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


def create_app(
    settings: Settings,
    *,
    resolve_model: Callable[[str], Model | str] = lambda name: name,
    live: Callable[[], Cluster] | None = None,
    load_recorded: Callable[[], recorded.Snapshot] = cache(recorded.load),
    tracer_provider: TracerProvider | None = None,
) -> Starlette:
    """The app. Tests pass a scripted model and fake clusters; `app_from_env` passes nothing."""
    # A fresh connection per request: a re-issued token is picked up, and
    # recorded mode works with no kubeconfig at all.
    live = live or (lambda: connect(settings.reader_kubeconfig))

    def cluster_for(mode: str) -> tuple[Cluster, Settings]:
        if mode == "live":
            return live(), settings
        snapshot = load_recorded()
        # The recorded cluster is its own world: the tools offer its namespaces.
        return snapshot.cluster, dataclasses.replace(settings, namespaces=snapshot.namespaces)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def models(request: Request) -> Response:
        if request.path_params["mode"] not in MODES:
            return _error(404, f"Unknown mode; use one of: {', '.join(MODES)}.", "not_found")
        return JSONResponse({"object": "list", "data": [{"id": settings.model, "object": "model", "owned_by": "diagnoster"}]})

    async def chat_completions(request: Request) -> Response:
        mode = request.path_params["mode"]
        if mode not in MODES:
            return _error(404, f"Unknown mode; use one of: {', '.join(MODES)}.", "not_found")
        try:
            body = await request.json()
            messages = [m for m in body["messages"] if isinstance(m, dict)]
        except (ValueError, KeyError, TypeError):
            return _error(400, "The body must be JSON with a `messages` list.", "invalid_request_error")
        questions = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
        if not questions or not questions[-1].strip():
            return _error(400, "No user message: the last user message is the question.", "invalid_request_error")
        prompts = [_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")]
        system_prompt = "\n\n".join(p for p in prompts if p.strip()) or None
        requested = body.get("model")
        model_name = requested if isinstance(requested, str) and MODEL_NAME.match(requested) else settings.model

        try:
            cluster, run_settings = cluster_for(mode)
        except FileNotFoundError as e:  # no kubeconfig (live) or no snapshot (recorded)
            return _error(503, str(e), "cluster_unavailable")
        try:
            report = await diagnose(
                questions[-1],
                run_settings,
                cluster,
                model=resolve_model(model_name),
                system_prompt=system_prompt,
                attributes={"diagnoster.mode": mode, "diagnoster.prompt_source": "request" if system_prompt else "code"},
                tracer_provider=tracer_provider,
                raise_model_errors=True,
            )
        except Exception as e:  # noqa: BLE001 - whatever stopped the model, the caller gets an error, not an answer
            log.warning("model error with %s: %s: %s", model_name, type(e).__name__, e)
            return _error(502, f"{type(e).__name__}: {e}", "model_error")

        content = render(report, model_name, settings.phoenix_url)
        base = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "created": int(time.time()), "model": model_name}
        if body.get("stream"):
            return StreamingResponse(_chunks(base, content), media_type="text/event-stream", headers={"cache-control": "no-cache"})
        choice = {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        return JSONResponse(base | {"object": "chat.completion", "choices": [choice]})

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        if tracer_provider is not None:
            tracer_provider.force_flush()

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/{mode}/v1/models", models),
            Route("/{mode}/v1/chat/completions", chat_completions, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def app_from_env() -> Starlette:
    """What uvicorn serves: `uvicorn diagnoster.server:app_from_env --factory`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    return create_app(settings, tracer_provider=setup(settings.phoenix_collector_endpoint))
