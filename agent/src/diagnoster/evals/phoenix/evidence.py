"""Phoenix code evaluator `evidence`: is every required fact backed by a call the agent cited?

A fact is backed when a cited tool call looked at one of the fact's sources
(a Kubernetes kind, a name pattern and an aspect such as logs or status). The
sources come from our code's record of each call, not from the model's words.
Supporting facts are reported but not graded.

What it does not check: that the fact's wording is true. A call that read
every pod in a namespace backs any pod-status fact. The check is that the
agent looked in the right place and cited it; whether the conclusion is
right is the `diagnosis` evaluator's job.

This file is uploaded to Phoenix as it is and runs there in a sandbox, so it
uses the standard library only and imports nothing from this repository.
`output` is the agent's reply; `reference` is the dataset row's expected answer.
"""

import json
from fnmatch import fnmatchcase


def _report(output):
    """The agent's RunReport: its reply is the report as JSON in a json code block.

    The last json block is read, so older replies (readable text, then the
    block) and plain JSON replies can still be re-graded.
    """
    if isinstance(output, dict) and "messages" in output:  # how Phoenix stores a Playground run
        output = output["messages"][0]["content"]  # the agent answers with one message; the judge reads the same
    if isinstance(output, str):
        text = output.strip()
        start, end = text.rfind("```json\n"), text.rfind("\n```")
        if start != -1 and end > start:
            text = text[start + len("```json\n") : end]
        try:
            output = json.loads(text)
        except ValueError:
            return None
    return output if isinstance(output, dict) and "status" in output else None


def _result(passed, explanation):
    return {"label": "pass" if passed else "fail", "score": 1.0 if passed else 0.0, "explanation": explanation}


def _backed(fact, calls):
    """The id of a cited call that looked at one of the fact's sources, if any."""
    for call in calls:
        for source in call.get("sources", []):
            for pattern in fact["any_of"]:
                if source["kind"] == pattern["kind"] and source["aspect"] == pattern["aspect"] and fnmatchcase(source["name"], pattern["name"]):
                    return call["id"]
    return None


def evaluate(output, reference):
    report = _report(output)
    if report is None:
        return _result(False, "no report found in the agent's reply")
    diagnosis = report.get("diagnosis")
    if diagnosis is None:
        return _result(False, f"run stopped ({report.get('stop_reason')}); nothing to grade")
    cited_ids = {cid for item in diagnosis["evidence"] for cid in item["tool_call_ids"]}
    unknown = cited_ids - {call["id"] for call in report["tool_calls"]}
    if unknown:
        return _result(False, f"cites calls that never happened: {', '.join(sorted(unknown))}")
    cited = [call for call in report["tool_calls"] if call["id"] in cited_ids]
    lines, missing = [], 0
    grading = reference["grading"]
    for fact in grading["required_evidence"]:
        by = _backed(fact, cited)
        missing += by is None
        lines.append(f"{'ok' if by else 'MISSING'}: {fact['fact']}" + (f" ({by})" if by else ""))
    for fact in grading.get("supporting_evidence", []):
        by = _backed(fact, cited)
        lines.append(f"supporting {'ok' if by else 'absent'}: {fact['fact']}" + (f" ({by})" if by else ""))
    return _result(missing == 0, "\n".join(lines))
