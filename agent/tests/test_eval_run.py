"""The eval runner without Phoenix or a model: dataset, evaluators, pass^k and the precondition."""

from types import SimpleNamespace as N

import pytest
from fakes import World, deployment
from test_graders import GOOD

from diagnoster.evals import run as evals
from diagnoster.evals.answer_key import load_all

KEYS = load_all()


def test_the_dataset_has_both_variants_of_every_scenario():
    examples = evals.build_examples(KEYS)
    assert len(examples) == 2 * len(KEYS)
    by = {(e["metadata"]["scenario"], e["metadata"]["variant"]): e["input"]["question"] for e in examples}
    assert by[("bad-probe", "original")] == KEYS["bad-probe"].question
    assert by[("bad-probe", "reworded")] == KEYS["bad-probe"].reworded
    assert all(set(e) == {"input", "output", "metadata"} for e in examples)


async def test_evaluators_turn_grades_into_phoenix_scores():
    ev = evals.make_evaluators(KEYS)
    output = GOOD["healthy"]  # a JSON-shaped RunReport, as Phoenix stores it
    meta = {"scenario": "healthy", "variant": "original"}
    assert ev["diagnosis"](output, meta) == {"score": 1.0, "label": "pass", "explanation": "none"}
    assert ev["evidence"](output, meta)["score"] == 1.0
    assert (await ev["action"](output, meta))["label"] == "pass"  # kind none on the healthy control: no judge call
    wrong = ev["diagnosis"](output, {"scenario": "bad-probe", "variant": "original"})
    assert wrong["score"] == 0.0 and wrong["label"] == "fail" and "expected bad_probe" in wrong["explanation"]


def _runs(spec):
    """spec: {(scenario, variant): [scores per repetition as dicts]} -> fake examples, task runs, evaluation runs."""
    examples, task_runs, evaluation_runs = [], [], []
    for n, ((sid, variant), reps) in enumerate(spec.items()):
        ex_id = f"ex{n}"
        examples.append({"id": ex_id, "metadata": {"scenario": sid, "variant": variant}})
        for r, scores in enumerate(reps, start=1):
            run_id = f"{ex_id}-r{r}"
            task_runs.append({"id": run_id, "dataset_example_id": ex_id, "repetition_number": r, **({"error": "boom"} if scores is None else {})})
            for name, score in (scores or {}).items():
                evaluation_runs.append(N(experiment_run_id=run_id, name=name, error=None, result={"score": score}))
    return examples, task_runs, evaluation_runs


ALL = {"diagnosis": 1.0, "evidence": 1.0, "action": 1.0}


def test_pass_k_needs_every_repetition_to_pass_every_grader():
    examples, tr, er = _runs(
        {
            ("healthy", "original"): [ALL, ALL, ALL],
            ("healthy", "reworded"): [ALL, ALL | {"evidence": 0.0}, ALL],  # one flaky grade
            ("bad-probe", "original"): [ALL, None, ALL],  # one task error
        }
    )
    rows = {(r["scenario"], r["variant"]): r for r in evals.summarize(examples, tr, er, 3)}
    assert rows[("healthy", "original")]["pass_k"] is True
    flaky = rows[("healthy", "reworded")]
    assert flaky["pass_k"] is False and flaky["evidence"] == 2 and flaky["diagnosis"] == 3
    assert rows[("bad-probe", "original")]["pass_k"] is False and rows[("bad-probe", "original")]["passed_runs"] == 2


def test_a_missing_repetition_or_score_is_a_fail():
    examples, tr, er = _runs({("healthy", "original"): [ALL, {"diagnosis": 1.0, "evidence": 1.0}]})
    row = evals.summarize(examples, tr, er, 3)[0]
    assert row["pass_k"] is False and row["runs"] == 2 and row["action"] == 1


def test_an_evaluator_error_is_a_fail():
    examples, tr, er = _runs({("healthy", "original"): [ALL]})
    er[0].error = "judge timed out"
    assert evals.summarize(examples, tr, er, 1)[0]["pass_k"] is False


def test_render_shows_per_grader_counts_and_totals_per_variant():
    examples, tr, er = _runs({("healthy", "original"): [ALL] * 3, ("healthy", "reworded"): [ALL, ALL, ALL | {"action": 0.0}]})
    text = evals.render(evals.summarize(examples, tr, er, 3), 3)
    assert "healthy            original  PASS" in text
    assert "healthy            reworded  fail" in text
    assert "pass^3 original: 1/1" in text and "pass^3 reworded: 0/1" in text and "pass^3 overall: 1/2" in text


def test_absent_scenarios_are_found_by_an_empty_namespace():
    w = World()
    w.deployments = [deployment("inventory", {"app": "inventory"})]

    class PerNamespace:
        def list_namespaced_deployment(self, ns):
            return N(items=w.deployments if ns == "inventory" else [])

    cluster = N(apps=PerNamespace())
    assert evals.absent_scenarios(cluster, KEYS) == ["bad-probe", "missing-config", "selector-mismatch"]


def test_unknown_scenario_is_rejected(capsys):
    assert evals.main(["--scenario", "nope"]) == 1
    assert "unknown scenario(s): nope" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["concurrency", "timeout", "retries"])
def test_experiment_settings_are_the_honest_ones(key):
    assert evals.EXPERIMENT_SETTINGS[key] == {"concurrency": 2, "timeout": 240, "retries": 0}[key]
