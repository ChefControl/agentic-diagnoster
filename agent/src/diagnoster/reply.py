"""The agent's answer as one chat message: JSON only, in a json code block.

The message is the RunReport (status, diagnosis, every tool call, trace id)
with three fields in front of it: the model that answered, the cluster it
read, and the link to the agent's own trace in Phoenix.

Phoenix shows a chat message as Markdown, which joins lines and drops the
indentation. The code block keeps the JSON as written, so an experiment's
output reads like its reference output. The evaluators read the block.

The trace link matters more than it looks. Phoenix calls the agent like any
chat model and sends no trace context, so this field is the only way from a
Playground run to the agent's own trace.
"""

import json

from .contract import RunReport


def render(report: RunReport, model: str, phoenix_url: str, cluster: str | None = None) -> str:
    trace_url = f"{phoenix_url}/redirects/traces/{report.trace_id}" if report.trace_id else None
    head = {"model": model, "cluster": cluster, "trace_url": trace_url}
    return "```json\n" + json.dumps(head | report.model_dump(mode="json"), indent=2) + "\n```"


def unwrap(content: str) -> str:
    """The JSON inside a reply."""
    text = content.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        return text[len("```json\n") : -len("\n```")]
    return text
