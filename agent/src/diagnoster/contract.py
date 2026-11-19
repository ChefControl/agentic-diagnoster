"""The shape of the agent's answer.

Two objects, with different authors:

- Diagnosis: written by the model. Root cause, evidence and next action.
- RunReport: written by our code around the run. Whether it finished, the
  diagnosis if it did, every tool call that really happened, the trace id,
  and what became of the fix the agent asked to apply.

The report is the answer; the trace is the story. Evidence cites tool-call ids
only, never sources: our code records each call's sources (which Kubernetes
objects, which aspect) from its arguments and result, so the model can't vouch
for itself.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .categories import CATEGORIES, Category

# "service/catalog", "deployment/payments-api": a Kubernetes kind and name.
RESOURCE_PATTERN = r"^[a-z]+/[a-z0-9][a-z0-9.-]*$"

# The model sees this in the JSON schema, so it picks a category by meaning,
# not by guessing from the id.
CATEGORY_HELP = "The root-cause category, one of:\n" + "\n".join(f"- {cid}: {desc}" for cid, desc in CATEGORIES.items())

ToolCallId = Annotated[str, Field(min_length=1)]


class _Strict(BaseModel):
    # Unknown fields are an error, so a misspelled field fails loudly instead
    # of being dropped.
    model_config = ConfigDict(extra="forbid")


class RootCause(_Strict):
    category: Category = Field(description=CATEGORY_HELP)
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
    tool_call_ids: list[ToolCallId] = Field(
        min_length=1,
        description="The tool calls whose output shows this fact",
    )


class ProposedFix(_Strict):
    # Read and written as "from", including in RunReport JSON for the graders and the UI.
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    target: str = Field(pattern=RESOURCE_PATTERN)
    field: str = Field(min_length=1, description='e.g. "readinessProbe.httpGet.path"')
    # "from" is a Python keyword, so the attribute is from_ and the JSON key is "from".
    from_: str = Field(alias="from")
    to: str

    @model_validator(mode="after")
    def _changes_something(self) -> "ProposedFix":
        if self.from_ == self.to:
            raise ValueError("a fix must change the value: from and to are the same")
        return self


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
    def _action_matches_fault(self) -> "Diagnosis":
        fault = self.root_cause.category != Category.none
        if fault and self.next_action.kind == "none":
            raise ValueError("a fault needs a next action: recommend or remediate")
        if not fault and self.next_action.kind != "none":
            raise ValueError("with no fault found, the next action kind is none")
        return self


class Source(_Strict):
    """What a tool call looked at, derived by our code from its arguments."""

    kind: str  # Kubernetes kind, e.g. "Pod"
    name: str
    aspect: str  # e.g. "status", "logs", "events", "spec", "data", "endpoints"


class ToolCall(_Strict):
    id: ToolCallId  # ours: "tc_1", "tc_2", ... in call order; the model cites these
    provider_id: str | None = None  # the model provider's own id for the call, for matching traces
    tool: str
    args: dict[str, Any]
    # Every object the call looked at. Empty when it looked at none (e.g. a
    # list of names); several when it read many pods at once.
    sources: list[Source] = []


StopReason = Literal["request_limit", "tool_call_limit", "token_limit", "timeout", "invalid_output", "model_error"]

# What became of a fix the agent asked to apply (its apply_fix tool):
# - asked: waiting for the person (the run is awaiting_approval)
# - not_asked: no one could approve it here (evals, the recorded cluster), so it was denied
# - not_allowed: our allowlist refused it before anyone was asked
# - denied: the person said no
# - stale: approved, but the live value had changed, so nothing was changed
# - fixed / not_fixed: approved and applied; fresh reads did or didn't show it working
RemediationStatus = Literal["asked", "not_asked", "not_allowed", "denied", "stale", "fixed", "not_fixed"]


class Remediation(_Strict):
    """Written by our code, never by the model: what happened to the fix."""

    status: RemediationStatus
    detail: str
    fix: ProposedFix | None = None
    # The agent's own words, given when it asked: what the change should do.
    expected_effect: str | None = None


class RunReport(_Strict):
    # awaiting_approval: the agent asked to apply its fix and waits for a person;
    # the diagnosis is the one it asked with.
    status: Literal["complete", "stopped", "awaiting_approval"]
    stop_reason: StopReason | None = None
    diagnosis: Diagnosis | None = None
    tool_calls: list[ToolCall] = []
    trace_id: str | None = None
    remediation: Remediation | None = None

    @model_validator(mode="after")
    def _status_matches(self) -> "RunReport":
        if self.status in ("complete", "awaiting_approval") and (self.diagnosis is None or self.stop_reason is not None):
            raise ValueError(f"a {self.status} run has a diagnosis and no stop_reason")
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
