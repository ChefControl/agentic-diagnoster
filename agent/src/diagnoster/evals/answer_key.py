"""Load a scenario's answer key, scenarios/<id>/expected.yaml."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..categories import Category
from ..config import REPO_ROOT

SCENARIOS_DIR = REPO_ROOT / "scenarios"


class _Key(BaseModel):
    # Reviewer-only fields (title, fault, comments' siblings) are ignored, not errors.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SourcePattern(_Key):
    kind: str
    name: str  # fnmatch pattern, e.g. "checkout-*"
    aspect: str


class Fact(_Key):
    fact: str
    any_of: list[SourcePattern] = Field(min_length=1)


class Evidence(_Key):
    required: list[Fact] = []
    supporting: list[Fact] = []


class Expected(_Key):
    category: Category
    resources: list[str]
    explanation: str


class UnsafeAction(_Key):
    action: str
    why: str


class Change(_Key):
    field: str
    from_: str = Field(alias="from")
    to: str


class Remediation(_Key):
    target: str
    change: Change
    verify: list[str] = []


class AnswerKey(_Key):
    scenario: str
    namespace: str
    question: str
    reworded: str  # the same problem in a user's words, no Kubernetes names
    expected: Expected
    evidence: Evidence
    safe_actions: list[str]
    unsafe_actions: list[UnsafeAction] = []
    remediation: Remediation | None = None


def load_key(scenario: str, scenarios_dir: Path = SCENARIOS_DIR) -> AnswerKey:
    path = scenarios_dir / scenario / "expected.yaml"
    return AnswerKey.model_validate(yaml.safe_load(path.read_text()))


def load_all(scenarios_dir: Path = SCENARIOS_DIR) -> dict[str, AnswerKey]:
    return {p.parent.name: load_key(p.parent.name, scenarios_dir) for p in sorted(scenarios_dir.glob("*/expected.yaml"))}
