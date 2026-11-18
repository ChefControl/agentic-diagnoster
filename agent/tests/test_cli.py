"""The CLI's readable summary."""

from diagnoster.cli import _summary
from diagnoster.contract import RunReport


def test_summary_shows_the_diagnosis_its_evidence_calls_and_the_trace_link():
    report = RunReport.model_validate(
        {
            "status": "complete",
            "trace_id": "ab" * 16,
            "tool_calls": [{"id": "tc_1", "tool": "get_service", "args": {"namespace": "catalog", "name": "catalog"}}],
            "diagnosis": {
                "root_cause": {"category": "selector_mismatch", "resource": "service/catalog", "explanation": "No match."},
                "evidence": [{"fact": "No endpoints", "tool_call_ids": ["tc_1"]}],
                "next_action": {"summary": "Fix the selector.", "kind": "recommend"},
            },
        }
    )
    text = _summary(report, "http://localhost:6006")
    assert "Category:  selector_mismatch" in text
    assert "[tc_1] get_service(namespace=catalog, name=catalog)" in text
    assert "Next:      recommend: Fix the selector." in text
    assert f"http://localhost:6006/redirects/traces/{'ab' * 16}" in text


def test_summary_of_a_stopped_run():
    report = RunReport(status="stopped", stop_reason="tool_call_limit")
    assert _summary(report, "http://x").startswith("Stopped: tool_call_limit after 0 tool calls.")
