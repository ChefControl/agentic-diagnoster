"""The eval dataset, built from scenarios/*/expected.yaml.

One row per scenario and question variant: the question as written, and the
same problem reworded the way a user would report it. Each row's reference
holds the expected answer twice over: lines a person reads in Phoenix, and
the fields the evaluators need (they run inside Phoenix and see only the row).
"""

from typing import Any

from .answer_key import AnswerKey, Fact

DATASET = "diagnoster-scenarios"
DESCRIPTION = "Built from scenarios/*/expected.yaml by phoenix-setup. Edit the YAML, not the rows."


def _facts(facts: list[Fact]) -> list[dict[str, Any]]:
    return [{"fact": f.fact, "any_of": [p.model_dump() for p in f.any_of]} for f in facts]


def reference(key: AnswerKey) -> dict[str, Any]:
    resources = " or ".join(key.expected.resources)
    unsafe = [f"- {u.action} (unsafe because: {u.why})" for u in key.unsafe_actions]
    allowed = "none"
    if key.remediation:
        change = key.remediation.change
        allowed = f"{key.remediation.target} {change.field}: {change.from_!r} -> {change.to!r}"
    return {
        # For the reader, and for the action judge.
        "answer": f"{key.expected.category} in {resources}" if resources else f"{key.expected.category}: no fault found",
        "why": " ".join(key.expected.explanation.split()),
        "safe_actions": "\n".join(f"- {a}" for a in key.safe_actions),
        "unsafe_actions": "\n".join(unsafe) or "- (none)",
        "allowed_fix": allowed,
        # For the diagnosis and evidence evaluators.
        "category": str(key.expected.category),
        "resources": key.expected.resources,
        "required_evidence": _facts(key.evidence.required),
        "supporting_evidence": _facts(key.evidence.supporting),
    }


def build_examples(keys: dict[str, AnswerKey]) -> list[dict[str, Any]]:
    """The id ("bad-probe/reworded") names the row; the splits filter by variant or by scenario."""
    examples = []
    for sid, key in sorted(keys.items()):
        for variant, question in (("original", key.question), ("reworded", key.reworded)):
            examples.append(
                {
                    "id": f"{sid}/{variant}",
                    # Only `question` reaches the agent; the rest labels the row.
                    "input": {"scenario": sid, "variant": variant, "question": question},
                    "output": reference(key),
                    "metadata": {"scenario": sid, "variant": variant},
                    "splits": [variant, sid],
                }
            )
    return examples
