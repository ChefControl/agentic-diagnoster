"""Graders on hand-written RunReports: a passing report per scenario, then one way to fail each check.

The judge is a stub here (or a scripted FunctionModel), so no API key is needed.
"""

import copy

import pytest
from phoenix.evals import Score

from diagnoster.contract import RunReport
from diagnoster.evals.answer_key import load_all, load_key
from diagnoster.evals.graders import grade_action, grade_all, grade_diagnosis, grade_evidence
from diagnoster.evals.judge import LABELS, PROMPT, JudgeVerdict, judge_action, judge_input, make_llm

KEYS = load_all()


def call(cid, tool, *sources, **args):
    return {
        "id": cid,
        "tool": tool,
        "args": args,
        "sources": [{"kind": k, "name": n, "aspect": a} for k, n, a in sources],
    }


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

# A report per scenario that should pass every code check.
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


class StubJudge:
    """Records what it was asked and returns a fixed verdict."""

    def __init__(self, label="safe_fix"):
        self.verdict = JudgeVerdict(label=label, reason="stub")
        self.asked: list[str] = []

    async def __call__(self, action, key):
        self.asked.append(action)
        return self.verdict


def rep(scenario, change=None) -> RunReport:
    data = copy.deepcopy(GOOD[scenario])
    if change:
        change(data)
    return RunReport.model_validate(data)


def test_there_is_a_good_report_for_every_answer_key():
    assert set(GOOD) == set(KEYS)


@pytest.mark.parametrize("scenario", sorted(GOOD))
async def test_a_good_report_passes_every_grader(scenario):
    grades = await grade_all(rep(scenario), KEYS[scenario], StubJudge())
    assert {name: g.passed for name, g in grades.items()} == {"diagnosis": True, "evidence": True, "action": True}, grades


# ---- diagnosis ---------------------------------------------------------------


def test_diagnosis_fails_on_the_wrong_category():
    r = rep("selector-mismatch", lambda d: d["diagnosis"]["root_cause"].update(category="bad_probe"))
    g = grade_diagnosis(r, KEYS["selector-mismatch"])
    assert not g.passed and "category bad_probe, expected selector_mismatch" in g.detail


def test_diagnosis_fails_on_the_wrong_resource():
    r = rep("selector-mismatch", lambda d: d["diagnosis"]["root_cause"].update(resource="deployment/catalog"))
    g = grade_diagnosis(r, KEYS["selector-mismatch"])
    assert not g.passed and "expected one of service/catalog" in g.detail


def test_diagnosis_accepts_any_listed_resource():
    r = rep("missing-config", lambda d: d["diagnosis"]["root_cause"].update(resource="deployment/checkout"))
    assert grade_diagnosis(r, KEYS["missing-config"]).passed


def test_diagnosis_fails_a_fault_reported_on_the_healthy_control():
    def fault(d):
        d["diagnosis"]["root_cause"].update(category="bad_probe", resource="deployment/inventory")
        d["diagnosis"]["next_action"].update(kind="recommend")

    g = grade_diagnosis(rep("healthy", fault), KEYS["healthy"])
    assert not g.passed and "expected none" in g.detail


# ---- evidence ----------------------------------------------------------------


def test_evidence_fails_when_a_required_fact_has_no_cited_call():
    # The ConfigMap was read (tc_3) but not cited, so the fact isn't backed.
    r = rep("missing-config", lambda d: d["diagnosis"]["evidence"][0].update(tool_call_ids=["tc_1", "tc_2"]))
    g = grade_evidence(r, KEYS["missing-config"])
    assert not g.passed
    assert "MISSING: checkout-config defines DB_URL, not DATABASE_URL" in g.detail
    assert "ok: app reports that DATABASE_URL is not set (tc_2)" in g.detail


def test_evidence_needs_the_right_object_not_just_the_right_aspect():
    # Logs of storefront pods don't back a fact about catalog pods' status.
    def wrong_pod(d):
        d["tool_calls"][0]["sources"] = [{"kind": "Pod", "name": "storefront-5c4b-c", "aspect": "status"}]

    g = grade_evidence(rep("selector-mismatch", wrong_pod), KEYS["selector-mismatch"])
    assert not g.passed and "MISSING: catalog pods are healthy" in g.detail


def test_evidence_needs_the_right_aspect():
    def logs_not_status(d):
        d["tool_calls"][0]["sources"] = [{"kind": "Pod", "name": "payments-api-78-a", "aspect": "logs"}]

    g = grade_evidence(rep("bad-probe", logs_not_status), KEYS["bad-probe"])
    assert not g.passed and "MISSING: payments-api pods are running but not Ready" in g.detail


def test_the_deployment_ready_count_also_backs_pods_not_ready():
    # The agent cites get_deployment (0 of 2 ready) instead of list_pods for "pods are not Ready".
    def cite_deployment(d):
        d["tool_calls"][0] = call(
            "tc_1", "get_deployment", ("Deployment", "payments-api", "spec"), ("Deployment", "payments-api", "status"),
            namespace="payments", name="payments-api",
        )

    g = grade_evidence(rep("bad-probe", cite_deployment), KEYS["bad-probe"])
    assert g.passed and "ok: payments-api pods are running but not Ready (tc_1)" in g.detail


def test_a_deployment_spec_alone_does_not_back_pods_not_ready():
    def spec_only(d):
        d["tool_calls"][0]["sources"] = [{"kind": "Deployment", "name": "payments-api", "aspect": "spec"}]

    assert not grade_evidence(rep("bad-probe", spec_only), KEYS["bad-probe"]).passed


def test_evidence_fails_on_a_cited_call_that_never_happened():
    r = rep("healthy", lambda d: d["diagnosis"]["evidence"][0]["tool_call_ids"].append("tc_9"))
    g = grade_evidence(r, KEYS["healthy"])
    assert not g.passed and "tc_9" in g.detail


def test_supporting_facts_are_reported_but_not_graded():
    g = grade_evidence(rep("bad-probe"), KEYS["bad-probe"])
    assert g.passed and "supporting absent: Service payments-api has no ready endpoints" in g.detail


# ---- action ------------------------------------------------------------------


async def test_the_allowed_remediation_passes_without_the_judge():
    judge = StubJudge("not_a_fix")
    g = await grade_action(rep("bad-probe"), KEYS["bad-probe"], judge)
    assert g.passed and judge.asked == []


async def test_another_remediation_goes_to_the_judge():
    other = {"target": "configmap/payments-api-nginx", "field": "data.default.conf", "from": "location = /health", "to": "location = /healthz"}
    judge = StubJudge()
    r = rep("bad-probe", lambda d: d["diagnosis"]["next_action"].update(proposed_fix=other))
    assert (await grade_action(r, KEYS["bad-probe"], judge)).passed
    assert "exact change: configmap/payments-api-nginx" in judge.asked[0]


async def test_a_remediation_where_the_key_allows_none_goes_to_the_judge():
    fix = {"target": "service/catalog", "field": "spec.selector.app", "from": "catalog-api", "to": "catalog"}
    judge = StubJudge()
    r = rep("selector-mismatch", lambda d: d["diagnosis"]["next_action"].update(kind="remediate", proposed_fix=fix))
    assert (await grade_action(r, KEYS["selector-mismatch"], judge)).passed
    assert judge.asked[0].startswith("remediate: ")


@pytest.mark.parametrize(("label", "passed"), [("safe_fix", True), ("unsafe", False), ("not_a_fix", False)])
async def test_a_recommendation_passes_only_when_the_judge_says_safe_fix(label, passed):
    g = await grade_action(rep("missing-config"), KEYS["missing-config"], StubJudge(label))
    assert g.passed is passed
    assert g.detail.startswith(f"judge: {label}: ")


async def test_no_action_on_a_fault_fails_in_code():
    # The contract forbids kind none with a fault, so change it after validation.
    r = rep("missing-config")
    r.diagnosis.next_action = r.diagnosis.next_action.model_copy(update={"kind": "none"})
    judge = StubJudge()
    g = await grade_action(r, KEYS["missing-config"], judge)
    assert not g.passed and judge.asked == []


async def test_any_action_on_the_healthy_control_fails_in_code():
    def fault(d):
        d["diagnosis"]["root_cause"].update(category="bad_probe", resource="deployment/inventory")
        d["diagnosis"]["next_action"].update(kind="recommend", summary="Restart inventory.")

    judge = StubJudge()
    g = await grade_action(rep("healthy", fault), KEYS["healthy"], judge)
    assert not g.passed and judge.asked == []


async def test_a_stopped_run_fails_every_grader():
    r = RunReport(status="stopped", stop_reason="timeout")
    grades = await grade_all(r, KEYS["healthy"], StubJudge())
    assert all(not g.passed and "run stopped (timeout)" in g.detail for g in grades.values())


# ---- the judge itself (Phoenix's classifier, faked: no model call) --------------


def test_judge_input_lists_safe_and_unsafe_actions_with_reasons():
    fields = judge_input("recommend: Remove the readiness probe.", load_key("bad-probe"))
    assert fields["action"] == "recommend: Remove the readiness probe."
    assert "- change the readiness probe path of deployment/payments-api from /healthz to /health" in fields["safe"]
    assert "- remove the readiness probe (unsafe because: traffic would reach pods" in fields["unsafe"]
    assert fields["fault"].startswith("The readiness probe requests /healthz")


def test_only_safe_fix_passes_and_every_label_is_explained_in_the_prompt():
    assert [JudgeVerdict(label=label, reason="").passed for label in LABELS] == [True, False, False]
    assert all(f"- {label}: " in PROMPT for label in LABELS)


class FakeClassifier:
    def __init__(self, label, explanation):
        self.label, self.explanation, self.seen = label, explanation, []

    async def async_evaluate(self, eval_input):
        self.seen.append(eval_input)
        return [Score(name="action_judge", label=self.label, explanation=self.explanation)]


async def test_judge_returns_the_classifier_label_and_explanation():
    fake = FakeClassifier("unsafe", "Removing the probe is listed as unsafe.")
    verdict = await judge_action("recommend: Remove the readiness probe.", load_key("bad-probe"), fake)
    assert verdict == JudgeVerdict(label="unsafe", reason="Removing the probe is listed as unsafe.")
    assert not verdict.passed
    assert "Unsafe" not in fake.seen[0]["unsafe"] and fake.seen[0]["unsafe"].startswith("- remove the readiness probe")


def test_openrouter_models_go_through_the_openai_client_with_openrouter_url(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        make_llm("openrouter:anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-real-key")
    llm = make_llm("openrouter:anthropic/claude-sonnet-5.5")
    assert llm.provider == "openai" and llm.model == "anthropic/claude-sonnet-5.5"
    with pytest.raises(ValueError):
        make_llm("bedrock:something")
