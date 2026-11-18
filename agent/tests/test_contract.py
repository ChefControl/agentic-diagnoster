"""The contract accepts one well-formed answer per scenario and rejects each
broken rule on its own."""

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from diagnoster.categories import CATEGORIES, Category, load_categories
from diagnoster.contract import Diagnosis, RunReport, ToolCall, unknown_tool_call_ids

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def diagnosis(category, resource, kind="recommend", proposed_fix=None, tool_call_ids=("tc_1",)):
    return {
        "root_cause": {"category": category, "resource": resource, "explanation": "Why, in one or two sentences."},
        "evidence": [{"fact": "Something the tool call showed", "tool_call_ids": list(tool_call_ids)}],
        "next_action": {"summary": "What to do next", "kind": kind, "proposed_fix": proposed_fix},
    }


BAD_PROBE_FIX = {
    "target": "deployment/payments-api",
    "field": "readinessProbe.httpGet.path",
    "from": "/healthz",
    "to": "/health",
}

VALID = {
    "selector-mismatch": diagnosis("selector_mismatch", "service/catalog"),
    "missing-config": diagnosis("missing_config", "configmap/checkout-config", tool_call_ids=["tc_1", "tc_2"]),
    "bad-probe": diagnosis("bad_probe", "deployment/payments-api", kind="remediate", proposed_fix=BAD_PROBE_FIX),
    "healthy": diagnosis("none", None, kind="none"),
}


@pytest.mark.parametrize("scenario", VALID)
def test_accepts_a_diagnosis_for_each_scenario(scenario):
    d = Diagnosis.model_validate(VALID[scenario])
    expected = yaml.safe_load((SCENARIOS / scenario / "expected.yaml").read_text())["expected"]
    assert d.root_cause.category == expected["category"]


def test_bad_probe_fix_mirrors_the_answer_key():
    remediation = yaml.safe_load((SCENARIOS / "bad-probe" / "expected.yaml").read_text())["remediation"]
    fix = Diagnosis.model_validate(VALID["bad-probe"]).next_action.proposed_fix
    assert fix is not None
    assert fix.target == remediation["target"]
    assert {"field": fix.field, "from": fix.from_, "to": fix.to} == remediation["change"]


def test_fix_serializes_with_the_json_key_from():
    # Graders and the UI read RunReport JSON, so "from" must survive a plain dump.
    report = RunReport.model_validate({"status": "complete", "diagnosis": VALID["bad-probe"]})
    fix = json.loads(report.model_dump_json())["diagnosis"]["next_action"]["proposed_fix"]
    assert fix["from"] == "/healthz"
    assert "from_" not in fix


def test_the_model_sees_what_each_category_means():
    schema = json.dumps(Diagnosis.model_json_schema())
    for description in CATEGORIES.values():
        assert description in schema


def broken(scenario, change):
    data = copy.deepcopy(VALID[scenario])
    change(data)
    return data


INVALID = {
    "unknown category": broken("selector-mismatch", lambda d: d["root_cause"].update(category="cosmic_rays")),
    "resource with no fault": broken("healthy", lambda d: d["root_cause"].update(resource="service/inventory")),
    "fault without a resource": broken("selector-mismatch", lambda d: d["root_cause"].update(resource=None)),
    "resource not kind/name": broken("selector-mismatch", lambda d: d["root_cause"].update(resource="catalog")),
    "empty explanation": broken("selector-mismatch", lambda d: d["root_cause"].update(explanation="")),
    "explanation too long": broken("selector-mismatch", lambda d: d["root_cause"].update(explanation="x" * 601)),
    "remediate without a fix": broken("bad-probe", lambda d: d["next_action"].update(proposed_fix=None)),
    "fix without remediate": broken("bad-probe", lambda d: d["next_action"].update(kind="recommend")),
    "no fault but an action": broken("healthy", lambda d: d["next_action"].update(kind="recommend")),
    "fault but no action": broken("selector-mismatch", lambda d: d["next_action"].update(kind="none")),
    "fix that changes nothing": broken("bad-probe", lambda d: d["next_action"]["proposed_fix"].update(to="/healthz")),
    "empty tool call id": broken("selector-mismatch", lambda d: d["evidence"][0].update(tool_call_ids=[""])),
    "no evidence": broken("selector-mismatch", lambda d: d.update(evidence=[])),
    "evidence citing no tool call": broken("selector-mismatch", lambda d: d["evidence"][0].update(tool_call_ids=[])),
    "unknown field": broken("selector-mismatch", lambda d: d.update(confidence=0.9)),
}


@pytest.mark.parametrize("case", INVALID)
def test_rejects(case):
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(INVALID[case])


TOOL_CALLS = [
    {"id": "tc_1", "provider_id": "call_abc", "tool": "get_service", "args": {"namespace": "catalog", "name": "catalog"},
     "sources": [{"kind": "Service", "name": "catalog", "aspect": "spec"},
                 {"kind": "Service", "name": "catalog", "aspect": "endpoints"}]},
]


@pytest.mark.parametrize("sources", [[], [{"kind": "Pod", "name": "a-1", "aspect": "status"}],
                                     [{"kind": "Pod", "name": f"a-{i}", "aspect": "logs"} for i in range(3)]])
def test_tool_call_has_zero_one_or_many_sources(sources):
    call = ToolCall.model_validate({"id": "tc_2", "tool": "list_pods", "args": {"namespace": "shop"}, "sources": sources})
    assert len(call.sources) == len(sources)
    assert call.provider_id is None


def test_tool_call_rejects_the_old_single_source_field():
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"id": "tc_1", "tool": "t", "args": {}, "source": {"kind": "Pod", "name": "a", "aspect": "logs"}})


def test_complete_report():
    report = RunReport.model_validate(
        {"status": "complete", "diagnosis": VALID["selector-mismatch"], "tool_calls": TOOL_CALLS, "trace_id": "abc"}
    )
    assert unknown_tool_call_ids(report) == set()


def test_stopped_report():
    report = RunReport.model_validate({"status": "stopped", "stop_reason": "tool_call_limit", "tool_calls": TOOL_CALLS})
    assert report.diagnosis is None
    assert unknown_tool_call_ids(report) == set()


@pytest.mark.parametrize(
    "report",
    [
        {"status": "complete"},  # no diagnosis
        {"status": "complete", "diagnosis": VALID["healthy"], "stop_reason": "timeout"},
        {"status": "stopped"},  # no reason
        {"status": "stopped", "stop_reason": "timeout", "diagnosis": VALID["healthy"]},
        {"status": "stopped", "stop_reason": "out_of_coffee"},
    ],
)
def test_report_status_matches_its_contents(report):
    with pytest.raises(ValidationError):
        RunReport.model_validate(report)


def test_finds_cited_ids_no_tool_call_has():
    d = diagnosis("selector_mismatch", "service/catalog", tool_call_ids=["tc_1", "tc_9"])
    report = RunReport.model_validate({"status": "complete", "diagnosis": d, "tool_calls": TOOL_CALLS})
    assert unknown_tool_call_ids(report) == {"tc_9"}


def test_categories_match_the_yaml():
    data = yaml.safe_load((SCENARIOS / "categories.yaml").read_text())
    ids = [c["id"] for c in data["categories"]]
    assert list(CATEGORIES) == ids
    assert [c.value for c in Category] == ids


def test_every_answer_key_uses_a_known_category():
    for expected in SCENARIOS.glob("*/expected.yaml"):
        assert yaml.safe_load(expected.read_text())["expected"]["category"] in CATEGORIES, expected


def test_categories_file_comes_from_the_environment(tmp_path, monkeypatch):
    other = tmp_path / "categories.yaml"
    other.write_text("categories:\n  - {id: only_one, description: The only category}\n")
    monkeypatch.setenv("CATEGORIES_FILE", str(other))
    assert load_categories() == {"only_one": "The only category"}


def test_missing_categories_file_says_how_to_fix_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CATEGORIES_FILE", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError, match="CATEGORIES_FILE"):
        load_categories()
