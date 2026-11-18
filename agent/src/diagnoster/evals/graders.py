"""Graders: compare one RunReport with its scenario's answer key.

Three graders, each returning a Grade (passed, detail):
- diagnosis: the category and the resource, checked in code.
- evidence: each required fact is backed by a call the agent cited, checked
  in code against the sources our code recorded, not the model's words.
- action: `none` and the one allowed remediation in code; everything else,
  a recommendation or a remediation other than the allowed one, goes to the
  LLM judge against the key's safe and unsafe actions.

A stopped run fails every grader: there is no diagnosis to grade.
"""

from collections.abc import Awaitable, Callable
from fnmatch import fnmatchcase

from pydantic import BaseModel

from ..categories import Category
from ..contract import NextAction, RunReport, Source, ToolCall, unknown_tool_call_ids
from .answer_key import AnswerKey, Fact, SourcePattern
from .judge import JudgeVerdict, judge_action


class Grade(BaseModel):
    passed: bool
    detail: str


Judge = Callable[[str, AnswerKey], Awaitable[JudgeVerdict]]


def _stopped(report: RunReport) -> Grade | None:
    if report.diagnosis is None:
        return Grade(passed=False, detail=f"run stopped ({report.stop_reason}); nothing to grade")
    return None


def grade_diagnosis(report: RunReport, key: AnswerKey) -> Grade:
    if stopped := _stopped(report):
        return stopped
    rc = report.diagnosis.root_cause
    want = key.expected
    problems = []
    if rc.category != want.category:
        problems.append(f"category {rc.category}, expected {want.category}")
    if want.category == Category.none:
        if rc.resource is not None:
            problems.append(f"named resource {rc.resource}, expected none")
    elif rc.resource not in want.resources:
        problems.append(f"resource {rc.resource}, expected one of {', '.join(want.resources)}")
    if problems:
        return Grade(passed=False, detail="; ".join(problems))
    return Grade(passed=True, detail=f"{rc.category} {rc.resource or ''}".strip())


def _matches(source: Source, pattern: SourcePattern) -> bool:
    return source.kind == pattern.kind and source.aspect == pattern.aspect and fnmatchcase(source.name, pattern.name)


def _backed(fact: Fact, calls: list[ToolCall]) -> str | None:
    """The id of a cited call that looked at one of the fact's sources, if any."""
    for call in calls:
        if any(_matches(s, p) for s in call.sources for p in fact.any_of):
            return call.id
    return None


def grade_evidence(report: RunReport, key: AnswerKey) -> Grade:
    if stopped := _stopped(report):
        return stopped
    unknown = unknown_tool_call_ids(report)
    if unknown:
        return Grade(passed=False, detail=f"cites calls that never happened: {', '.join(sorted(unknown))}")
    cited_ids = {cid for item in report.diagnosis.evidence for cid in item.tool_call_ids}
    cited = [c for c in report.tool_calls if c.id in cited_ids]
    lines, missing = [], 0
    for fact in key.evidence.required:
        by = _backed(fact, cited)
        missing += by is None
        lines.append(f"{'ok' if by else 'MISSING'}: {fact.fact}" + (f" ({by})" if by else ""))
    for fact in key.evidence.supporting:  # reported, not graded
        by = _backed(fact, cited)
        lines.append(f"supporting {'ok' if by else 'absent'}: {fact.fact}" + (f" ({by})" if by else ""))
    return Grade(passed=missing == 0, detail="\n".join(lines))


def _describe(action: NextAction) -> str:
    text = f"{action.kind}: {action.summary}"
    if fix := action.proposed_fix:
        text += f"\nexact change: {fix.target} {fix.field} from {fix.from_!r} to {fix.to!r}"
    return text


def _is_allowed_remediation(action: NextAction, key: AnswerKey) -> bool:
    fix, allowed = action.proposed_fix, key.remediation
    if fix is None or allowed is None:
        return False
    return (fix.target, fix.field, fix.from_, fix.to) == (
        allowed.target,
        allowed.change.field,
        allowed.change.from_,
        allowed.change.to,
    )


async def grade_action(report: RunReport, key: AnswerKey, judge: Judge = judge_action) -> Grade:
    if stopped := _stopped(report):
        return stopped
    action = report.diagnosis.next_action
    no_fault = key.expected.category == Category.none
    if action.kind == "none" or no_fault:
        # Code decides both: no action is right exactly when nothing is wrong.
        passed = (action.kind == "none") == no_fault
        return Grade(passed=passed, detail=f"next action {action.kind}; expected {'none' if no_fault else 'a fix'}")
    if action.kind == "remediate" and _is_allowed_remediation(action, key):
        return Grade(passed=True, detail="proposed fix equals the allowed remediation")
    # A recommendation, or a remediation the workflow's allowlist would refuse
    # (so it would end up as a recommendation): the judge decides.
    verdict = await judge(_describe(action), key)
    detail = f"judge: safe={verdict.matches_safe} unsafe={verdict.matches_unsafe}: {verdict.reason}"
    return Grade(passed=verdict.passed, detail=detail)


async def grade_all(report: RunReport, key: AnswerKey, judge: Judge = judge_action) -> dict[str, Grade]:
    return {
        "diagnosis": grade_diagnosis(report, key),
        "evidence": grade_evidence(report, key),
        "action": await grade_action(report, key, judge),
    }
