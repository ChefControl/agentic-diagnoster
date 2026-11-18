"""The eval dataset, built from scenarios/*/expected.yaml.

One row per scenario and question variant: the question as written, and the
same problem reworded the way a user would report it.

Each row's reference has the shape of the agent's own answer, so the two sit
side by side in Phoenix: `status`, then `diagnosis` with its root cause,
evidence and next action. Below it, `grading` holds what the evaluators need
and a person doesn't (they run inside Phoenix and see only the row).
"""

from typing import Any

from .answer_key import AnswerKey, Fact

DATASET = "diagnoster-scenarios"
DESCRIPTION = "Built from scenarios/*/expected.yaml by phoenix-setup. Edit the YAML, not the rows."


def _facts(facts: list[Fact]) -> list[dict[str, Any]]:
    return [{"fact": f.fact, "any_of": [p.model_dump() for p in f.any_of]} for f in facts]


def reference(key: AnswerKey) -> dict[str, Any]:
    want = key.expected
    fix = None
    if key.remediation:
        change = key.remediation.change
        fix = {"target": key.remediation.target, "field": change.field, "from": change.from_, "to": change.to}
    unsafe = [f"- {u.action} (unsafe because: {u.why})" for u in key.unsafe_actions]
    return {
        # The expected answer, in the agent's own shape.
        "status": "complete",
        "diagnosis": {
            "root_cause": {
                "category": str(want.category),
                # Any one of these is correct.
                "resource": " or ".join(want.resources) or None,
                "explanation": " ".join(want.explanation.split()),
            },
            "evidence": [f.fact for f in key.evidence.required],
            "next_action": {
                "safe_actions": key.safe_actions,
                "unsafe_actions": [u.action for u in key.unsafe_actions],
                # The one change the agent may apply itself, after approval.
                "proposed_fix": fix,
            },
        },
        # For the evaluators.
        "grading": {
            "resources": want.resources,
            "required_evidence": _facts(key.evidence.required),
            "supporting_evidence": _facts(key.evidence.supporting),
            # The judge's prompt takes plain text.
            "safe_actions": "\n".join(f"- {a}" for a in key.safe_actions),
            "unsafe_actions": "\n".join(unsafe) or "- (none)",
            "allowed_fix": f"{fix['target']} {fix['field']}: {fix['from']!r} -> {fix['to']!r}" if fix else "none",
        },
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
