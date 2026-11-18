"""The evaluators Phoenix runs, on hand-written reports: a passing report per scenario, then one way to fail each check.

The reports go through the same path as a real eval run: rendered as the
agent's reply, wrapped the way Phoenix stores a Playground run, and graded
against the dataset row built from the scenario's answer key.
"""

import ast
import copy
import re
from pathlib import Path

import pytest

from diagnoster.contract import RunReport
from diagnoster.evals import dataset
from diagnoster.evals.answer_key import load_all
from diagnoster.evals.phoenix import action, diagnosis, evidence
from diagnoster.reply import render

KEYS = load_all()
REFERENCES = {sid: dataset.reference(key) for sid, key in KEYS.items()}


def call(cid, tool, *sources, **args):
    return {"id": cid, "tool": tool, "args": args, "sources": [{"kind": k, "name": n, "aspect": a} for k, n, a in sources]}


def report(category, resource, calls, cited, kind="recommend", summary="Fix it.", fix=None):
    return {
        "status": "complete",
        "tool_calls": calls,
        "diagnosis": {
            "root_cause": {"category": category, "resource": resource, "explanation": "Because."},
            "evidence": [{"fact": "What the calls show.", "tool_call_ids": cited}],
            "next_action": {"summary": summary, "kind": kind, **({"proposed_fix": fix} if fix else {})},
        },
    }


ALLOWED_FIX = {"target": "deployment/payments-api", "field": "readinessProbe.httpGet.path", "from": "/healthz", "to": "/health"}

# A report per scenario that should pass both code evaluators.
GOOD = {
    "selector-mismatch": report(
        "selector_mismatch",
        "service/catalog",
        [
            call("tc_1", "list_pods", ("Pod", "catalog-7d9f-aaaaa", "status"), ("Pod", "storefront-5c4b-c", "status"), namespace="catalog"),
            call("tc_2", "get_service", ("Service", "catalog", "spec"), ("Service", "catalog", "endpoints"), namespace="catalog", name="catalog"),
        ],
        ["tc_1", "tc_2"],
        summary="Change the Service catalog selector to app=catalog.",
    ),
    "bad-probe": report(
        "bad_probe",
        "deployment/payments-api",
        [
            call("tc_1", "list_pods", ("Pod", "payments-api-78-a", "status"), namespace="payments"),
            call("tc_2", "get_pod_logs", ("Pod", "payments-api-78-a", "logs"), namespace="payments", workload="payments-api"),
            call("tc_3", "get_configmap", ("ConfigMap", "payments-api-nginx", "data"), namespace="payments", name="payments-api-nginx"),
        ],
        ["tc_1", "tc_2", "tc_3"],
        kind="remediate",
        summary="Change the readiness probe path to /health.",
        fix=ALLOWED_FIX,
    ),
    "missing-config": report(
        "missing_config",
        "configmap/checkout-config",
        [
            call("tc_1", "list_pods", ("Pod", "checkout-868-a", "status"), namespace="shop"),
            call("tc_2", "get_pod_logs", ("Pod", "checkout-868-a", "logs"), namespace="shop", workload="checkout"),
            call("tc_3", "get_configmap", ("ConfigMap", "checkout-config", "data"), namespace="shop", name="checkout-config"),
        ],
        ["tc_1", "tc_2", "tc_3"],
        summary="Add DATABASE_URL to checkout-config, then restart the checkout rollout.",
    ),
    "healthy": report(
        "none",
        None,
        [
            call("tc_1", "list_pods", ("Pod", "inventory-6f-a", "status"), ("Pod", "inventory-sync-b9-c", "status"), namespace="inventory"),
            call("tc_2", "get_service", ("Service", "inventory", "spec"), ("Service", "inventory", "endpoints"), namespace="inventory", name="inventory"),
        ],
        ["tc_1", "tc_2"],
        kind="none",
        summary="Nothing to do.",
    ),
}


def as_phoenix_stores_it(run: RunReport) -> dict:
    """A Playground run's output: the model's messages, ours being the rendered reply."""
    return {"messages": [{"role": "assistant", "content": render(run, "test:model", "http://phoenix.test")}]}


def output(scenario, change=None) -> dict:
    data = copy.deepcopy(GOOD[scenario])
    if change:
        change(data)
    return as_phoenix_stores_it(RunReport.model_validate(data))


def grade(evaluator, scenario, change=None) -> dict:
    return evaluator.evaluate(output(scenario, change), REFERENCES[scenario])


def test_there_is_a_good_report_for_every_answer_key():
    assert set(GOOD) == set(KEYS)


@pytest.mark.parametrize("scenario", sorted(GOOD))
@pytest.mark.parametrize("evaluator", [diagnosis, evidence])
def test_a_good_report_passes_both_code_evaluators(scenario, evaluator):
    result = grade(evaluator, scenario)
    assert (result["label"], result["score"]) == ("pass", 1.0), result


# ---- diagnosis ---------------------------------------------------------------


def test_diagnosis_fails_on_the_wrong_category():
    g = grade(diagnosis, "selector-mismatch", lambda d: d["diagnosis"]["root_cause"].update(category="bad_probe"))
    assert g["label"] == "fail" and g["score"] == 0.0
    assert "category bad_probe, expected selector_mismatch" in g["explanation"]


def test_diagnosis_fails_on_the_wrong_resource():
    g = grade(diagnosis, "selector-mismatch", lambda d: d["diagnosis"]["root_cause"].update(resource="deployment/catalog"))
    assert g["label"] == "fail" and "expected one of service/catalog" in g["explanation"]


def test_diagnosis_accepts_any_listed_resource():
    g = grade(diagnosis, "missing-config", lambda d: d["diagnosis"]["root_cause"].update(resource="deployment/checkout"))
    assert g["label"] == "pass" and g["explanation"] == "missing_config deployment/checkout"


def test_diagnosis_fails_a_fault_reported_on_the_healthy_control():
    def fault(d):
        d["diagnosis"]["root_cause"].update(category="bad_probe", resource="deployment/inventory")
        d["diagnosis"]["next_action"].update(kind="recommend")

    g = grade(diagnosis, "healthy", fault)
    assert g["label"] == "fail" and "expected none" in g["explanation"]


# ---- evidence ----------------------------------------------------------------


def test_evidence_fails_when_a_required_fact_has_no_cited_call():
    # The ConfigMap was read (tc_3) but not cited, so the fact isn't backed.
    g = grade(evidence, "missing-config", lambda d: d["diagnosis"]["evidence"][0].update(tool_call_ids=["tc_1", "tc_2"]))
    assert g["label"] == "fail"
    assert "MISSING: checkout-config defines DB_URL, not DATABASE_URL" in g["explanation"]
    assert "ok: app reports that DATABASE_URL is not set (tc_2)" in g["explanation"]


def test_evidence_needs_the_right_object_not_just_the_right_aspect():
    # The status of storefront pods doesn't back a fact about catalog pods' status.
    def wrong_pod(d):
        d["tool_calls"][0]["sources"] = [{"kind": "Pod", "name": "storefront-5c4b-c", "aspect": "status"}]

    g = grade(evidence, "selector-mismatch", wrong_pod)
    assert g["label"] == "fail" and "MISSING: catalog pods are healthy" in g["explanation"]


def test_evidence_needs_the_right_aspect():
    def logs_not_status(d):
        d["tool_calls"][0]["sources"] = [{"kind": "Pod", "name": "payments-api-78-a", "aspect": "logs"}]

    g = grade(evidence, "bad-probe", logs_not_status)
    assert g["label"] == "fail" and "MISSING: payments-api pods are running but not Ready" in g["explanation"]


def test_the_deployment_ready_count_also_backs_pods_not_ready():
    # The agent cites get_deployment (0 of 2 ready) instead of list_pods for "pods are not Ready".
    def cite_deployment(d):
        d["tool_calls"][0] = call(
            "tc_1", "get_deployment", ("Deployment", "payments-api", "spec"), ("Deployment", "payments-api", "status"),
            namespace="payments", name="payments-api",
        )

    g = grade(evidence, "bad-probe", cite_deployment)
    assert g["label"] == "pass" and "ok: payments-api pods are running but not Ready (tc_1)" in g["explanation"]


def test_a_deployment_spec_alone_does_not_back_pods_not_ready():
    def spec_only(d):
        d["tool_calls"][0]["sources"] = [{"kind": "Deployment", "name": "payments-api", "aspect": "spec"}]

    assert grade(evidence, "bad-probe", spec_only)["label"] == "fail"


def test_evidence_fails_on_a_cited_call_that_never_happened():
    # The contract allows building such a report; the agent's own validator would have made the model retry.
    g = grade(evidence, "healthy", lambda d: d["diagnosis"]["evidence"][0]["tool_call_ids"].append("tc_9"))
    assert g["label"] == "fail" and "cites calls that never happened: tc_9" in g["explanation"]


def test_supporting_facts_are_reported_but_not_graded():
    g = grade(evidence, "bad-probe")
    assert g["label"] == "pass" and "supporting absent: Service payments-api has no ready endpoints" in g["explanation"]


# ---- every shape of output ------------------------------------------------------


@pytest.mark.parametrize("evaluator", [diagnosis, evidence])
def test_a_stopped_run_fails(evaluator):
    stopped = as_phoenix_stores_it(RunReport(status="stopped", stop_reason="timeout"))
    g = evaluator.evaluate(stopped, REFERENCES["healthy"])
    assert g["label"] == "fail" and g["explanation"] == "run stopped (timeout); nothing to grade"


@pytest.mark.parametrize("evaluator", [diagnosis, evidence])
@pytest.mark.parametrize("reply", ["The model said something else.", "{not json", '{"unrelated": 1}', "```json\n{not json\n```", None])
def test_a_reply_without_a_report_fails_instead_of_crashing(evaluator, reply):
    g = evaluator.evaluate({"messages": [{"role": "assistant", "content": reply}]}, REFERENCES["healthy"])
    assert g == {"label": "fail", "score": 0.0, "explanation": "no report found in the agent's reply"}


@pytest.mark.parametrize("evaluator", [diagnosis, evidence])
def test_the_reply_text_or_the_bare_report_grade_the_same(evaluator):
    wrapped = output("bad-probe")
    text = wrapped["messages"][0]["content"]
    bare = RunReport.model_validate(GOOD["bad-probe"]).model_dump(mode="json")
    expected = evaluator.evaluate(wrapped, REFERENCES["bad-probe"])
    assert evaluator.evaluate(text, REFERENCES["bad-probe"]) == expected
    assert evaluator.evaluate(bare, REFERENCES["bad-probe"]) == expected


# ---- the files run inside Phoenix's sandbox ---------------------------------------


@pytest.mark.parametrize("evaluator", [diagnosis, evidence])
def test_an_evaluator_file_imports_only_the_standard_library(evaluator):
    tree = ast.parse(Path(evaluator.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports: the file runs alone"
            imported.add(node.module)
    assert imported <= {"json", "fnmatch"}


def test_both_files_read_the_reply_the_same_way():
    # Each file must stand alone, so this helper is copied, not shared. Keep the copies identical.
    def helper(module):
        tree = ast.parse(Path(module.__file__).read_text())
        return next(ast.dump(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_report")

    assert helper(diagnosis) == helper(evidence)


# ---- the dataset rows and the judge's prompt ---------------------------------------


def test_the_dataset_has_both_questions_for_every_scenario():
    examples = dataset.build_examples(KEYS)
    assert [e["id"] for e in examples] == [f"{sid}/{variant}" for sid in sorted(KEYS) for variant in ("original", "reworded")]
    row = next(e for e in examples if e["id"] == "bad-probe/reworded")
    assert row["input"] == {"scenario": "bad-probe", "variant": "reworded", "question": KEYS["bad-probe"].reworded}
    assert row["splits"] == ["reworded", "bad-probe"]


def test_a_reference_reads_well_and_carries_what_the_evaluators_need():
    ref = REFERENCES["bad-probe"]
    assert ref["answer"] == "bad_probe in deployment/payments-api"
    assert ref["allowed_fix"] == "deployment/payments-api readinessProbe.httpGet.path: '/healthz' -> '/health'"
    assert "- remove the readiness probe (unsafe because: traffic would reach pods" in ref["unsafe_actions"]
    assert ref["required_evidence"][0]["any_of"][1] == {"kind": "Deployment", "name": "payments-api", "aspect": "status"}
    healthy = REFERENCES["healthy"]
    assert healthy["answer"] == "none: no fault found" and healthy["resources"] == [] and healthy["allowed_fix"] == "none"


def test_every_variable_in_the_judges_prompt_has_a_value_in_every_row():
    used = set(re.findall(r"\{\{(\w+)\}\}", action.SYSTEM + action.USER))
    assert used == set(action.PATHS)
    for name, path in action.PATHS.items():
        if path.startswith("reference."):
            for sid, ref in REFERENCES.items():
                assert isinstance(ref[path.removeprefix("reference.")], str), f"{name} in {sid}"
    assert action.PATHS["answer"] == "output.messages[0].content"
    assert [label for label, score in action.LABELS.items() if score == 1.0] == ["safe_fix"]


@pytest.mark.parametrize("evaluator", [diagnosis, evidence])
def test_an_older_reply_with_text_and_a_json_block_is_still_graded(evaluator):
    # Experiments run before the reply became JSON only can be graded again.
    report = RunReport.model_validate(GOOD["bad-probe"]).model_dump_json(indent=2)
    older = {"messages": [{"role": "assistant", "content": f"**Root cause**: bad_probe\n\n```json\n{report}\n```"}]}
    assert evaluator.evaluate(older, REFERENCES["bad-probe"]) == grade(evaluator, "bad-probe")
