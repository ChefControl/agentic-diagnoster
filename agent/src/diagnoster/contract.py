"""The shape of the agent's answer.

Two objects, with different authors:

- Diagnosis: written by the model. Root cause, evidence and next action.
- RunReport: written by our code around the run. Whether it finished, the
  diagnosis if it did, every tool call that really happened, and the trace id.

The report is the answer; the trace is the story. Evidence cites tool-call ids
only, never sources: our code derives each call's source (which Kubernetes
object, which aspect) from its arguments, so the model can't vouch for itself.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .categories import Category

# "service/catalog", "deployment/payments-api": a Kubernetes kind and name.
RESOURCE_PATTERN = r"^[a-z]+/[a-z0-9][a-z0-9.-]*$"


class _Strict(BaseModel):
    # Unknown fields are an error, so a misspelled field fails loudly instead
    # of being dropped.
    model_config = ConfigDict(extra="forbid")


class RootCause(_Strict):
    category: Category
    resource: str | None = Field(
        pattern=RESOURCE_PATTERN,
        description='The faulty object as "kind/name", e.g. "service/catalog"; null when no fault is found',
    )
    explanation: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def _resource_iff_fault(self) -> "RootCause":
        if self.category == Category.none and self.resource is not None:
            raise ValueError("no fault found (category none) names no resource")
        if self.category != Category.none and self.resource is None:
            raise ValueError("a fault needs the resource it is in")
        return self


class EvidenceItem(_Strict):
    fact: str = Field(min_length=1)
    tool_call_ids: list[str] = Field(
        min_length=1,
        description="The tool calls whose output shows this fact",
    )


class ProposedFix(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target: str = Field(pattern=RESOURCE_PATTERN)
    field: str = Field(min_length=1, description='e.g. "readinessProbe.httpGet.path"')
    # "from" is a Python keyword, so the attribute is from_ and the JSON key is "from".
    from_: str = Field(alias="from")
    to: str


class NextAction(_Strict):
    summary: str = Field(min_length=1)
    kind: Literal["none", "recommend", "remediate"] = Field(
        description="none: nothing to do; recommend: a person should act; remediate: the agent can apply proposed_fix after approval",
    )
    proposed_fix: ProposedFix | None = None

    @model_validator(mode="after")
    def _fix_iff_remediate(self) -> "NextAction":
        if (self.kind == "remediate") != (self.proposed_fix is not None):
            raise ValueError("proposed_fix is given exactly when kind is remediate")
        return self


class Diagnosis(_Strict):
    root_cause: RootCause
    evidence: list[EvidenceItem] = Field(min_length=1)
    next_action: NextAction

    @model_validator(mode="after")
    def _nothing_to_do_without_fault(self) -> "Diagnosis":
        if self.root_cause.category == Category.none and self.next_action.kind != "none":
            raise ValueError("with no fault found, the next action kind is none")
        return self


class Source(_Strict):
    """What a tool call looked at, derived by our code from its arguments."""

    kind: str  # Kubernetes kind, e.g. "Pod"
    name: str
    aspect: str  # e.g. "status", "logs", "events", "spec", "data", "endpoints"


class ToolCall(_Strict):
    id: str
    tool: str
    args: dict[str, Any]
    source: Source | None = None  # None when the call looked at no single object


StopReason = Literal["request_limit", "tool_call_limit", "timeout", "invalid_output", "model_error"]


class RunReport(_Strict):
    status: Literal["complete", "stopped"]
    stop_reason: StopReason | None = None
    diagnosis: Diagnosis | None = None
    tool_calls: list[ToolCall] = []
    trace_id: str | None = None

    @model_validator(mode="after")
    def _status_matches(self) -> "RunReport":
        if self.status == "complete" and (self.diagnosis is None or self.stop_reason is not None):
            raise ValueError("a complete run has a diagnosis and no stop_reason")
        if self.status == "stopped" and (self.diagnosis is not None or self.stop_reason is None):
            raise ValueError("a stopped run has a stop_reason and no diagnosis")
        return self


def unknown_tool_call_ids(report: RunReport) -> set[str]:
    """Ids the evidence cites that no tool call in the run has."""
    if report.diagnosis is None:
        return set()
    known = {call.id for call in report.tool_calls}
    cited = {cid for item in report.diagnosis.evidence for cid in item.tool_call_ids}
    return cited - known
