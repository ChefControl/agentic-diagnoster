"""The evals on real agent output, with no model call.

tests/replies/ holds the agent's actual replies from one eval run in Phoenix
(gpt-6-luna, every scenario, both wordings of the question; trace links
removed). Each one is graded by the two code evaluators against the answer
key, exactly as Phoenix grades it. The action judge needs a model, so it runs
in Phoenix only.

Two things this proves in CI:
- the evaluators pass the agent's real, correct answers (not only the
  hand-written reports in test_evaluators.py);
- they would catch a wrong one: each reply fails every other scenario's key.

When the reply format or an answer key changes, refresh the files from a new
eval run rather than editing them by hand.
"""

import json
from pathlib import Path

import pytest

from diagnoster.contract import RunReport
from diagnoster.evals import dataset
from diagnoster.evals.answer_key import load_all
from diagnoster.evals.phoenix import diagnosis, evidence

REPLIES = sorted((Path(__file__).parent / "replies").glob("*.json"))
REFERENCES = {sid: dataset.reference(key) for sid, key in load_all().items()}
HEAD = ("model", "cluster", "trace_url")


def scenario_of(path: Path) -> str:
    return path.stem.split("--")[0]


def phoenix_run(path: Path) -> dict:
    """The reply as Phoenix stores a Playground run's output."""
    return {"messages": [{"role": "assistant", "content": path.read_text()}]}


def test_there_is_a_real_reply_for_both_questions_of_every_scenario():
    assert sorted(p.stem for p in REPLIES) == sorted(f"{s}--{v}" for s in REFERENCES for v in ("original", "reworded"))


@pytest.mark.parametrize("path", REPLIES, ids=lambda p: p.stem)
def test_a_real_reply_is_a_valid_report(path):
    reply = json.loads(path.read_text())
    RunReport.model_validate({k: v for k, v in reply.items() if k not in HEAD})


@pytest.mark.parametrize("path", REPLIES, ids=lambda p: p.stem)
@pytest.mark.parametrize("evaluator", [diagnosis, evidence], ids=["diagnosis", "evidence"])
def test_a_real_reply_passes_its_own_answer_key(path, evaluator):
    result = evaluator.evaluate(phoenix_run(path), REFERENCES[scenario_of(path)])
    assert result["label"] == "pass", result["explanation"]


@pytest.mark.parametrize("path", REPLIES, ids=lambda p: p.stem)
def test_a_real_reply_fails_every_other_scenarios_key(path):
    for other, reference in REFERENCES.items():
        if other != scenario_of(path):
            assert diagnosis.evaluate(phoenix_run(path), reference)["label"] == "fail", other
