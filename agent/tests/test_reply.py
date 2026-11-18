"""The reply text: readable on top, the exact report at the end."""

from diagnoster.contract import RunReport
from diagnoster.reply import render

COMPLETE = RunReport.model_validate(
    {
        "status": "complete",
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "tool_calls": [
            {"id": "tc_1", "tool": "get_deployment", "args": {"namespace": "payments", "name": "payments-api"}},
            {"id": "tc_2", "tool": "get_events", "args": {"namespace": "payments", "workload": None}},
        ],
        "diagnosis": {
            "root_cause": {
                "category": "bad_probe",
                "resource": "deployment/payments-api",
                "explanation": "The probe asks for /healthz; the app serves /health.",
            },
            "evidence": [
                {"fact": "The readiness probe requests /healthz.", "tool_call_ids": ["tc_1"]},
                {"fact": "The probe fails with 404, with a ```json fence in the text.", "tool_call_ids": ["tc_2", "tc_1"]},
            ],
            "next_action": {
                "summary": "Point the probe at /health.",
                "kind": "remediate",
                "proposed_fix": {
                    "target": "deployment/payments-api",
                    "field": "readinessProbe.httpGet.path",
                    "from": "/healthz",
                    "to": "/health",
                },
            },
        },
    }
)
STOPPED = RunReport(status="stopped", stop_reason="timeout", tool_calls=COMPLETE.tool_calls)


def report_in(text: str) -> RunReport:
    block = text[text.rindex("```json\n") + len("```json\n") : text.rindex("\n```")]
    return RunReport.model_validate_json(block)


def test_the_summary_reads_top_down_and_names_the_cited_calls():
    text = render(COMPLETE, "openrouter:openai/gpt-6-luna", "http://localhost:6006")
    summary = text[: text.rindex("```json")]
    assert summary.splitlines()[:2] == [
        "**Root cause**: bad_probe in deployment/payments-api",
        "The probe asks for /healthz; the app serves /health.",
    ]
    assert "- The readiness probe requests /healthz. [tc_1 get_deployment(namespace=payments, name=payments-api)]" in summary
    assert "tc_2 get_events(namespace=payments); tc_1 get_deployment(" in summary  # a None argument is left out
    assert "**Next action** (remediate): Point the probe at /health." in summary
    assert "Proposed fix: deployment/payments-api readinessProbe.httpGet.path: '/healthz' -> '/health'" in summary
    assert "Model: openrouter:openai/gpt-6-luna" in summary
    assert "Trace: http://localhost:6006/redirects/traces/0af7651916cd43dd8448eb211c80319c" in summary


def test_the_last_json_block_is_the_exact_report():
    # Even when the model's own text contains a json fence, the last block is ours.
    assert report_in(render(COMPLETE, "m", "http://p")) == COMPLETE
    assert report_in(render(STOPPED, "m", "http://p")) == STOPPED


def test_a_stopped_run_says_why_and_a_run_without_tracing_has_no_trace_line():
    text = render(STOPPED, "m", "http://p")
    assert text.startswith("**Stopped**: timeout after 2 tool calls. No diagnosis.")
    assert "Trace:" not in text


def test_no_fault_names_no_resource():
    healthy = RunReport.model_validate(
        {
            "status": "complete",
            "tool_calls": [{"id": "tc_1", "tool": "list_pods", "args": {"namespace": "inventory"}}],
            "diagnosis": {
                "root_cause": {"category": "none", "resource": None, "explanation": "Everything is Ready."},
                "evidence": [{"fact": "All pods are Ready.", "tool_call_ids": ["tc_1"]}],
                "next_action": {"summary": "Nothing to do.", "kind": "none"},
            },
        }
    )
    assert render(healthy, "m", "http://p").startswith("**Root cause**: none\nEverything is Ready.")
