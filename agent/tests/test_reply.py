"""The reply: JSON only, the report plus the model, the cluster and the trace link."""

import json

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
                {"fact": "The probe fails with 404.", "tool_call_ids": ["tc_2", "tc_1"]},
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
HEAD = ("model", "cluster", "trace_url")


def report_of(reply: dict) -> RunReport:
    return RunReport.model_validate({k: v for k, v in reply.items() if k not in HEAD})


def test_the_reply_is_json_only():
    text = render(COMPLETE, "openai:gpt-6-luna", "http://localhost:6006", cluster="live")
    reply = json.loads(text)  # the whole message parses: no text around it
    assert list(reply)[:3] == list(HEAD)
    assert reply["model"] == "openai:gpt-6-luna" and reply["cluster"] == "live"
    assert reply["trace_url"] == "http://localhost:6006/redirects/traces/0af7651916cd43dd8448eb211c80319c"


def test_the_rest_is_the_exact_report():
    assert report_of(json.loads(render(COMPLETE, "m", "http://p"))) == COMPLETE
    assert report_of(json.loads(render(STOPPED, "m", "http://p"))) == STOPPED


def test_a_run_without_tracing_has_no_trace_link():
    assert json.loads(render(STOPPED, "m", "http://p"))["trace_url"] is None
