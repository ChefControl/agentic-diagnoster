"""The eval runner without Phoenix or a model: dataset, evaluators, pass^k and the precondition."""

import asyncio
from types import SimpleNamespace as N
from unittest.mock import patch

import pytest
from fakes import World, deployment, selector_mismatch_world
from pydantic_ai.models.function import FunctionModel
from test_graders import GOOD, StubJudge

from diagnoster.agent import diagnose
from diagnoster.config import Settings
from diagnoster.contract import RunReport
from diagnoster.evals import run as evals
from diagnoster.evals.answer_key import load_all

KEYS = load_all()


def test_the_dataset_has_both_variants_of_every_scenario():
    examples = evals.build_examples(KEYS)
    assert len(examples) == 2 * len(KEYS)
    by = {e["id"]: e for e in examples}
    assert by["bad-probe/original"]["input"] == {"scenario": "bad-probe", "variant": "original", "question": KEYS["bad-probe"].question}
    assert by["bad-probe/reworded"]["input"]["question"] == KEYS["bad-probe"].reworded
    assert by["bad-probe/reworded"]["splits"] == ["reworded", "bad-probe"]
    assert all(e["metadata"] == {"scenario": e["input"]["scenario"], "variant": e["input"]["variant"]} for e in examples)


def test_each_example_carries_the_expected_answer_for_the_reader():
    ref = evals.reference(KEYS["bad-probe"])
    assert ref["answer"] == "bad_probe on deployment/payments-api"
    assert "readiness probe fails on /healthz with 404" in ref["required_evidence"]
    assert ref["allowed_fix"] == "deployment/payments-api readinessProbe.httpGet.path: /healthz -> /health"
    assert evals.reference(KEYS["missing-config"])["answer"] == "missing_config on deployment/checkout or configmap/checkout-config"
    healthy = evals.reference(KEYS["healthy"])
    assert healthy["answer"] == "none: no fault found" and "allowed_fix" not in healthy


def test_the_stored_output_reads_top_down_with_the_full_report_last():
    out = evals.readable(RunReport.model_validate(GOOD["bad-probe"]))
    assert list(out) == ["answer", "why", "next", "tool_calls", "report"]
    assert out["answer"] == "bad_probe on deployment/payments-api"
    assert out["next"] == "remediate: Change the readiness probe path to /health."
    assert out["tool_calls"] == 3 and out["report"]["status"] == "complete"
    stopped = evals.readable(RunReport(status="stopped", stop_reason="timeout"))
    assert stopped["answer"] == "stopped: timeout" and "why" not in stopped


def out(scenario):
    return evals.readable(RunReport.model_validate(GOOD[scenario]))


async def score(evaluator, output, scenario):
    return await evaluator.async_evaluate(output=output, metadata={"scenario": scenario, "variant": "original"})


async def test_evaluators_turn_grades_into_phoenix_scores():
    diagnosis, evidence, action, passed = evals.make_evaluators(KEYS, StubJudge())
    assert [e.name for e in (diagnosis, evidence, action, passed)] == ["diagnosis", "evidence", "action", "passed"]
    assert [e.kind for e in (diagnosis, evidence, action, passed)] == ["CODE", "CODE", "LLM", "CODE"]
    assert await score(diagnosis, out("healthy"), "healthy") == {"score": 1.0, "label": "pass", "explanation": "none"}
    assert (await score(evidence, out("healthy"), "healthy"))["score"] == 1.0
    assert (await score(passed, out("healthy"), "healthy"))["explanation"] == "all three graders passed"
    wrong = await score(diagnosis, out("healthy"), "bad-probe")
    assert wrong["score"] == 0.0 and wrong["label"] == "fail" and "expected bad_probe" in wrong["explanation"]


async def test_passed_names_the_failed_graders_and_asks_the_judge_only_once():
    judge = StubJudge("unsafe")
    _, _, action, passed = evals.make_evaluators(KEYS, judge)
    output = out("missing-config")  # a recommendation, so the judge decides
    results = await asyncio.gather(score(action, output, "missing-config"), score(passed, output, "missing-config"))
    assert results[0]["label"] == "fail"
    assert results[1] == {"score": 0.0, "label": "fail", "explanation": "failed: action"}
    assert len(judge.asked) == 1


async def test_a_model_error_raises_in_the_task_so_it_is_not_graded_as_an_answer():
    def broken(messages, info):
        raise RuntimeError("429 rate limited")

    seen = {}

    async def fake_diagnose(question, settings, cluster, **kw):
        seen.update(kw, question=question)
        return await diagnose(question, settings, cluster, model=FunctionModel(broken), **kw)

    task = evals.make_task(Settings(namespaces=("catalog",)), selector_mismatch_world().cluster())
    with patch.object(evals, "diagnose", fake_diagnose), pytest.raises(RuntimeError, match="429"):
        await task({"scenario": "x", "variant": "original", "question": "Why?"})
    assert seen == {"raise_model_errors": True, "question": "Why?"}  # only the question reaches the agent


def _runs(spec):
    """spec: {(scenario, variant): [scores per repetition as dicts]} -> fake examples, task runs, evaluation runs."""
    examples, task_runs, evaluation_runs = [], [], []
    for n, ((sid, variant), reps) in enumerate(spec.items()):
        ex_id = f"ex{n}"
        examples.append({"id": f"{sid}/{variant}", "node_id": ex_id, "metadata": {"scenario": sid, "variant": variant}})
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
    assert "model error" not in text


def test_runs_lost_to_model_errors_are_counted_apart_from_wrong_answers():
    examples, tr, er = _runs({("bad-probe", "reworded"): [ALL, None, None]})
    row = evals.summarize(examples, tr, er, 3)[0]
    assert row["errors"] == 2 and row["passed_runs"] == 1 and row["pass_k"] is False
    text = evals.render([row], 3)
    assert text.splitlines()[1].endswith("  2")
    assert "2 run(s) hit a model error" in text


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
