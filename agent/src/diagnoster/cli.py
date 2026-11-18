"""`diagnose "question"`: run the agent once and print what it found.

Exit code 0 when the run completed, 2 when it stopped (a limit, a timeout,
an invalid answer or a model error), 1 when it couldn't start.
"""

import argparse
import asyncio
import sys

from .agent import diagnose
from .cluster import connect
from .config import load_settings
from .contract import RunReport
from .tracing import setup


def _summary(report: RunReport, phoenix_url: str) -> str:
    calls = {c.id: c for c in report.tool_calls}
    lines = []
    if report.diagnosis is None:
        lines.append(f"Stopped: {report.stop_reason} after {len(report.tool_calls)} tool calls.")
    else:
        d = report.diagnosis
        lines += [
            f"Category:  {d.root_cause.category}",
            f"Resource:  {d.root_cause.resource or '-'}",
            f"Why:       {d.root_cause.explanation}",
            "",
            "Evidence:",
        ]
        for item in d.evidence:
            lines.append(f"  - {item.fact}")
            for cid in item.tool_call_ids:
                c = calls.get(cid)
                args = ", ".join(f"{k}={v}" for k, v in c.args.items() if v is not None) if c else ""
                lines.append(f"      [{cid}] {c.tool}({args})" if c else f"      [{cid}] (unknown)")
        lines += ["", f"Next:      {d.next_action.kind}: {d.next_action.summary}"]
        fix = d.next_action.proposed_fix
        if fix:
            lines.append(f"Fix:       {fix.target} {fix.field}: {fix.from_!r} -> {fix.to!r}")
    if report.trace_id:
        lines += ["", f"Trace:     {phoenix_url}/redirects/traces/{report.trace_id}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="diagnose", description="Diagnose a problem in the cluster.")
    parser.add_argument("question", help='e.g. "Checkout in namespace shop is unhealthy. Why?"')
    parser.add_argument("--json", action="store_true", help="print the full RunReport as JSON")
    args = parser.parse_args(argv)

    settings = load_settings()
    try:
        cluster = connect(settings.reader_kubeconfig)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    provider = setup(settings.phoenix_collector_endpoint)
    try:
        report = asyncio.run(diagnose(args.question, settings, cluster))
    except Exception as e:  # e.g. a missing API key: the model can't even be built
        print(f"Could not start the run: {e}", file=sys.stderr)
        return 1
    finally:
        provider.force_flush()

    print(report.model_dump_json(indent=2) if args.json else _summary(report, settings.phoenix_url))
    return 0 if report.status == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
