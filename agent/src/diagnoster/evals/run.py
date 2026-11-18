"""`evals`: run every scenario question through the agent as a Phoenix experiment.

The work runs here (the agent needs the cluster, the kubeconfig and the API
key); Phoenix keeps the record: the dataset, every run's trace, and each
grader's score per run.

    uv run evals --model openrouter:openai/gpt-6-luna [--scenario healthy] [--repetitions 3]

Each (scenario, variant) passes only if all repetitions pass all three
graders: pass^k, a reliability measure, not best-of-k. Evals never change the
cluster: they grade the proposed action and never apply it. The scenarios must
already be deployed.
"""

import argparse
import asyncio
import dataclasses
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from phoenix.client import AsyncClient
from phoenix.client.experiments import create_evaluator

from ..agent import diagnose
from ..cluster import Cluster, connect
from ..config import REPO_ROOT, Settings, load_settings
from ..contract import RunReport
from ..tracing import setup
from .answer_key import AnswerKey, load_all
from .graders import Grade, Judge, grade_action, grade_diagnosis, grade_evidence
from .judge import judge_action, judge_model

DATASET = "diagnoster-scenarios"
GRADERS = ("diagnosis", "evidence", "action")
VARIANTS = ("original", "reworded")
# Honest settings: a failed run stays failed (no retries), and Phoenix's own
# timeout sits above the agent's 180 s limit so the agent stops cleanly first.
EXPERIMENT_SETTINGS = {"concurrency": 2, "timeout": 240, "retries": 0}


# ---- dataset ------------------------------------------------------------------


def _answer(category: str, resource: str | None) -> str:
    """One line for the reader: "bad_probe on deployment/payments-api", or "none: no fault found"."""
    return f"{category} on {resource}" if resource else f"{category}: no fault found"


def reference(key: AnswerKey) -> dict[str, Any]:
    """The expected answer, shown in Phoenix next to each run's output.

    For people reading the experiment: the graders read the answer key itself.
    """
    ref: dict[str, Any] = {
        "answer": _answer(key.expected.category, " or ".join(key.expected.resources)),
        "why": " ".join(key.expected.explanation.split()),
        "required_evidence": [fact.fact for fact in key.evidence.required],
        "safe_actions": key.safe_actions,
    }
    if key.remediation:
        change = key.remediation.change
        ref["allowed_fix"] = f"{key.remediation.target} {change.field}: {change.from_} -> {change.to}"
    return ref


def build_examples(keys: dict[str, AnswerKey]) -> list[dict[str, Any]]:
    """One example per (scenario, variant), rebuilt from the YAML every run.

    The id ("bad-probe/reworded") names the row in Phoenix. The splits let you
    filter by question variant or by scenario there, and let a `--scenario` run
    pick its examples without shrinking the dataset.
    """
    examples = []
    for sid, key in sorted(keys.items()):
        for variant, question in (("original", key.question), ("reworded", key.reworded)):
            examples.append(
                {
                    "id": f"{sid}/{variant}",
                    # Only `question` goes to the agent; the rest labels the row for the reader.
                    "input": {"scenario": sid, "variant": variant, "question": question},
                    "output": reference(key),
                    "metadata": {"scenario": sid, "variant": variant},
                    "splits": [variant, sid],
                }
            )
    return examples


def absent_scenarios(cluster: Cluster, keys: dict[str, AnswerKey]) -> list[str]:
    """Scenarios whose namespace has no Deployments, read as the reader."""
    return [sid for sid, key in sorted(keys.items()) if not cluster.apps.list_namespaced_deployment(key.namespace).items]


# ---- task and evaluators --------------------------------------------------------


def readable(report: RunReport) -> dict[str, Any]:
    """The task output Phoenix stores: a few readable lines first, the full report last."""
    if report.diagnosis is None:
        head = {"answer": f"stopped: {report.stop_reason}"}
    else:
        d = report.diagnosis
        head = {
            "answer": _answer(d.root_cause.category, d.root_cause.resource),
            "why": d.root_cause.explanation,
            "next": f"{d.next_action.kind}: {d.next_action.summary}",
        }
    return {**head, "tool_calls": len(report.tool_calls), "report": report.model_dump(mode="json")}


def make_task(settings: Settings, cluster: Cluster) -> Callable:
    async def task(input: dict[str, Any]) -> dict[str, Any]:
        # No `after`: evals never run the remediation workflow. A rate limit or
        # provider outage raises, so Phoenix records an error, not a wrong answer.
        report = await diagnose(input["question"], settings, cluster, raise_model_errors=True)
        return readable(report)

    return task


def _result(grade: Grade) -> dict[str, Any]:
    return {"score": 1.0 if grade.passed else 0.0, "label": "pass" if grade.passed else "fail", "explanation": grade.detail}


def make_evaluators(keys: dict[str, AnswerKey], judge: Judge = judge_action) -> list:
    """Four scores per run: our three graders, and `passed` (all three)."""
    action_grades: dict[str, asyncio.Task[Grade]] = {}

    def report_and_key(output: dict[str, Any], metadata: dict[str, Any]) -> tuple[RunReport, AnswerKey]:
        return RunReport.model_validate(output["report"]), keys[metadata["scenario"]]

    def action_grade(output: dict[str, Any], metadata: dict[str, Any]) -> asyncio.Task[Grade]:
        # `action` and `passed` both need it; share one task so the judge is asked once per run.
        report, key = report_and_key(output, metadata)
        run_id = report.trace_id or report.model_dump_json()
        if run_id not in action_grades:
            action_grades[run_id] = asyncio.ensure_future(grade_action(report, key, judge))
        return action_grades[run_id]

    @create_evaluator(kind="CODE", name="diagnosis")
    async def diagnosis(output, metadata):
        return _result(grade_diagnosis(*report_and_key(output, metadata)))

    @create_evaluator(kind="CODE", name="evidence")
    async def evidence(output, metadata):
        return _result(grade_evidence(*report_and_key(output, metadata)))

    @create_evaluator(kind="LLM", name="action")
    async def action(output, metadata):
        return _result(await action_grade(output, metadata))

    @create_evaluator(kind="CODE", name="passed")
    async def passed(output, metadata):
        report, key = report_and_key(output, metadata)
        grades = {
            "diagnosis": grade_diagnosis(report, key),
            "evidence": grade_evidence(report, key),
            "action": await action_grade(output, metadata),
        }
        failed = [name for name, grade in grades.items() if not grade.passed]
        detail = f"failed: {', '.join(failed)}" if failed else "all three graders passed"
        return _result(Grade(passed=not failed, detail=detail))

    return [diagnosis, evidence, action, passed]


# ---- pass^k ---------------------------------------------------------------------


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def summarize(examples: Iterable[dict[str, Any]], task_runs: list, evaluation_runs: list, repetitions: int) -> list[dict[str, Any]]:
    """Per (scenario, variant): passes per grader out of k, and pass^k.

    A run passes when all three graders scored it 1. A run with a task error
    (e.g. a rate limit), a missing score or an evaluator error counts as a
    fail for pass^k; `errors` says how many runs never produced an answer.
    """
    # Runs refer to an example by Phoenix's own id (node_id), not by the id we gave it.
    example_meta = {_get(e, "node_id") or _get(e, "id"): _get(e, "metadata") for e in examples}
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    for ev in evaluation_runs:
        result = _get(ev, "result")
        if _get(ev, "error") is None and result is not None:
            scores[_get(ev, "experiment_run_id")][_get(ev, "name")] = _get(result, "score") or 0.0
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for run in task_runs:
        meta = example_meta[_get(run, "dataset_example_id")]
        row = rows.setdefault(
            (meta["scenario"], meta["variant"]),
            {"scenario": meta["scenario"], "variant": meta["variant"], "runs": 0, "errors": 0, "passed_runs": 0, **{g: 0 for g in GRADERS}},
        )
        errored = bool(_get(run, "error"))
        run_scores = {} if errored else scores.get(_get(run, "id"), {})
        row["runs"] += 1
        row["errors"] += errored
        for g in GRADERS:
            row[g] += run_scores.get(g, 0.0) == 1.0
        row["passed_runs"] += all(run_scores.get(g, 0.0) == 1.0 for g in GRADERS)
    for row in rows.values():
        # Missing repetitions (a run that never came back) count as fails too.
        row["pass_k"] = row["runs"] == repetitions and row["passed_runs"] == repetitions
    return sorted(rows.values(), key=lambda r: (r["scenario"], VARIANTS.index(r["variant"])))


def render(rows: list[dict[str, Any]], repetitions: int) -> str:
    k = repetitions
    lines = [f"{'scenario':<18} {'variant':<9} {'pass^' + str(k):<7} " + "  ".join(f"{g:<9}" for g in GRADERS) + "  errors"]
    for r in rows:
        mark = "PASS" if r["pass_k"] else "fail"
        counts = "  ".join(f"{r[g]}/{k:<7}" for g in GRADERS)
        lines.append(f"{r['scenario']:<18} {r['variant']:<9} {mark:<7} {counts}  {r['errors'] or '-'}")
    lines.append("")
    for variant in (*VARIANTS, None):
        subset = [r for r in rows if variant is None or r["variant"] == variant]
        if subset:
            passed = sum(r["pass_k"] for r in subset)
            lines.append(f"pass^{k} {variant or 'overall'}: {passed}/{len(subset)}")
    errors = sum(r["errors"] for r in rows)
    if errors:
        lines.append(f"{errors} run(s) hit a model error (rate limit or provider outage) and gave no answer; they count as fails.")
    return "\n".join(lines)


# ---- main -----------------------------------------------------------------------


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run(settings: Settings, keys: dict[str, AnswerKey], selected: list[str], repetitions: int, cluster: Cluster) -> str:
    px = AsyncClient(base_url=settings.phoenix_url)
    # Always upload every scenario, so the dataset is the same whatever subset this run uses.
    dataset = await px.datasets.create_dataset(
        name=DATASET, examples=build_examples(keys), dataset_description="Built from scenarios/*/expected.yaml"
    )
    if set(selected) != set(keys):
        dataset = await px.datasets.get_dataset(dataset=DATASET, splits=selected)
    judge = judge_model()
    ran = await px.experiments.run_experiment(
        dataset=dataset,
        task=make_task(settings, cluster),
        evaluators=make_evaluators(keys),
        experiment_name=f"{settings.model} @ {_git_sha()}",
        experiment_metadata={"model": settings.model, "judge": judge, "git_sha": _git_sha(), "repetitions": repetitions},
        repetitions=repetitions,
        print_summary=False,
        **EXPERIMENT_SETTINGS,
    )
    rows = summarize(dataset.examples, ran["task_runs"], ran["evaluation_runs"], repetitions)
    url = px.experiments.get_experiment_url(dataset.id, ran["experiment_id"])
    header = f"model {settings.model}   judge {judge}   repetitions {repetitions}"
    return f"{header}\n\n{render(rows, repetitions)}\n\nPhoenix: {url}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description="Run the scenario evals as a Phoenix experiment.")
    parser.add_argument("--model", help="provider:model for the agent (default: MODEL)")
    parser.add_argument("--scenario", action="append", help="limit to these scenario ids (repeatable)")
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.model:
        settings = dataclasses.replace(settings, model=args.model)
    keys = load_all()
    selected = sorted(set(args.scenario or keys))
    unknown = sorted(set(selected) - set(keys))
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}; known: {', '.join(sorted(keys))}", file=sys.stderr)
        return 1

    try:
        cluster = connect(settings.reader_kubeconfig)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    absent = absent_scenarios(cluster, {sid: keys[sid] for sid in selected})
    if absent:
        print(f"Not deployed: {', '.join(absent)}. Deploy and wait first, from ui/:", file=sys.stderr)
        for sid in absent:
            print(f"  npm run scenarios -- deploy {sid} && npm run scenarios -- check {sid} --wait 180", file=sys.stderr)
        return 1

    provider = setup(settings.phoenix_collector_endpoint)
    try:
        print(asyncio.run(run(settings, keys, selected, args.repetitions, cluster)))
    finally:
        provider.force_flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
