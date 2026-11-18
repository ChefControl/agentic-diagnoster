"""The agent's answer as one chat message.

A person reads the summary at the top. A program reads the block at the end:
the full RunReport as JSON, in the message's last fenced `json` block.

The trace link matters more than it looks. Phoenix calls the agent like any
chat model and sends no trace context, so this line is the only way from a
Playground run to the agent's own trace.
"""

from .contract import RunReport, ToolCall


def _call(call: ToolCall | None, call_id: str) -> str:
    if call is None:
        return f"{call_id} (unknown)"
    args = ", ".join(f"{k}={v}" for k, v in call.args.items() if v is not None)
    return f"{call_id} {call.tool}({args})"


def _summary(report: RunReport) -> list[str]:
    if report.diagnosis is None:
        return [f"**Stopped**: {report.stop_reason} after {len(report.tool_calls)} tool calls. No diagnosis."]
    d = report.diagnosis
    calls = {c.id: c for c in report.tool_calls}
    where = f" in {d.root_cause.resource}" if d.root_cause.resource else ""
    lines = [f"**Root cause**: {d.root_cause.category}{where}", d.root_cause.explanation, "", "**Evidence**"]
    for item in d.evidence:
        cited = "; ".join(_call(calls.get(cid), cid) for cid in item.tool_call_ids)
        lines.append(f"- {item.fact} [{cited}]")
    lines += ["", f"**Next action** ({d.next_action.kind}): {d.next_action.summary}"]
    fix = d.next_action.proposed_fix
    if fix:
        lines.append(f"Proposed fix: {fix.target} {fix.field}: {fix.from_!r} -> {fix.to!r}")
    return lines


def render(report: RunReport, model: str, phoenix_url: str, cluster: str | None = None) -> str:
    lines = _summary(report) + ["", f"Model: {model}"]
    if cluster:
        lines.append(f"Cluster: {cluster}")
    if report.trace_id:
        lines.append(f"Trace: {phoenix_url}/redirects/traces/{report.trace_id}")
    lines += ["", "```json", report.model_dump_json(indent=2), "```"]
    return "\n".join(lines)
