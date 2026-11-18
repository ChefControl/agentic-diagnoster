"""Send traces to Phoenix with plain OpenTelemetry.

PydanticAI emits standard OTel spans for the run, each model request and each
tool call. Phoenix reads OpenInference attributes, so the OpenInference
processor translates each span before the exporter ships it. It must be added
first: processors run in order, and the exporter must see the translated span.
"""

from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent, InstrumentationSettings

SERVICE_NAME = "diagnoster-agent"
PROJECT_NAME = "diagnoster"  # Phoenix groups traces by this


def make_provider(exporter_processor: SpanProcessor) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME, "openinference.project.name": PROJECT_NAME})
    )
    provider.add_span_processor(OpenInferenceSpanProcessor())
    provider.add_span_processor(exporter_processor)
    return provider


def instrument(provider: TracerProvider) -> None:
    # include_content is on by default: Phoenix shows the prompts, the tool
    # arguments and the tool output exactly as the model saw it.
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider))


def setup(endpoint: str) -> TracerProvider:
    """Make the global tracer provider, exporting to Phoenix, and instrument every agent with it."""
    provider = make_provider(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    instrument(provider)
    return provider
