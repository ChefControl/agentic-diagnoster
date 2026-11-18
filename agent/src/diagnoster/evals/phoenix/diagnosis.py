"""Phoenix code evaluator `diagnosis`: did the agent name the expected root cause?

Passes when the category is the expected one and the resource is one of the
accepted ones (or none, on the healthy control).

This file is uploaded to Phoenix as it is and runs there in a sandbox, so it
uses the standard library only and imports nothing from this repository.
`output` is the agent's reply; `reference` is the dataset row's expected answer.
"""

import json


def _report(output):
    """The agent's RunReport: its reply is the report as JSON.

    Older replies had readable text first and the report in a final fenced
    json block; those are still read, so earlier experiments can be re-graded.
    """
    if isinstance(output, dict) and "messages" in output:  # how Phoenix stores a Playground run
        output = output["messages"][-1]["content"]
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


def evaluate(output, reference):
    report = _report(output)
    if report is None:
        return _result(False, "no report found in the agent's reply")
    diagnosis = report.get("diagnosis")
    if diagnosis is None:
        return _result(False, f"run stopped ({report.get('stop_reason')}); nothing to grade")
    category, resource = diagnosis["root_cause"]["category"], diagnosis["root_cause"]["resource"]
    problems = []
    if category != reference["category"]:
        problems.append(f"category {category}, expected {reference['category']}")
    if not reference["resources"]:
        if resource is not None:
            problems.append(f"named resource {resource}, expected none")
    elif resource not in reference["resources"]:
        problems.append(f"resource {resource}, expected one of {', '.join(reference['resources'])}")
    if problems:
        return _result(False, "; ".join(problems))
    return _result(True, f"{category} {resource or ''}".strip())
