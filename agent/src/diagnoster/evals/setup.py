"""`phoenix-setup`: put everything the evals need into the local Phoenix.

After it runs, an eval is a few clicks in Phoenix's Playground: pick the
dataset, pick the agent, press Run. It creates, by name:

- two models in the model picker, both our agent: `diagnoster-recorded`
  (replays the saved cluster) and `diagnoster-live` (reads the real one);
- the `openrouter` provider that Phoenix's own judge calls, holding the model
  API key from the environment;
- the prompt `diagnoster-system`: the agent's prompt, to edit and compare in Phoenix;
- the dataset `diagnoster-scenarios`, built from scenarios/*/expected.yaml;
- three evaluators on that dataset: `diagnosis` and `evidence` (our Python
  files, run in Phoenix's sandbox) and `action` (an LLM judge).

Safe to re-run: each part is looked up by name, left alone when it already
matches, and replaced when our files changed.

Providers, prompts and evaluators go through Phoenix's GraphQL API, the one
its own UI uses; it has no documented REST API for them. That is why Phoenix
is pinned to one version in compose.yaml.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..agent import SYSTEM_PROMPT
from ..config import load_settings
from . import dataset
from .answer_key import load_all
from .phoenix import action, diagnosis, evidence

RECORDED, LIVE, JUDGE_PROVIDER = "diagnoster-recorded", "diagnoster-live", "openrouter"
PROMPT = "diagnoster-system"
# In the Playground's model box: any name that isn't provider:model means the agent's default MODEL.
DEFAULT_MODEL_NAME = "default"
SANDBOX = "default-wasm-python"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
PASS_FAIL = {"pass": 1.0, "fail": 0.0}
CODE_EVALUATORS = {
    "diagnosis": ("Did the agent name the expected root cause: category and resource?", diagnosis),
    "evidence": ("Is every required fact backed by a tool call the agent cited?", evidence),
}


class SetupError(Exception):
    pass


class Phoenix:
    """The few HTTP calls the setup makes. Standard library only."""

    def __init__(self, url: str, secrets: tuple[str, ...] = ()):
        self.url = url.rstrip("/")
        self._secrets = tuple(s for s in secrets if s)

    def _scrub(self, text: str) -> str:
        """An error message must never carry the API key, whatever Phoenix echoes back."""
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.url}{path}", json.dumps(body).encode(), {"content-type": "application/json", "accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise SetupError(self._scrub(f"Phoenix answered {e.code} on {path}: {e.read().decode(errors='replace')[:300]}")) from None

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.post("/graphql", {"query": query, "variables": variables or {}})
        if result.get("errors"):
            raise SetupError(self._scrub("; ".join(e.get("message", "?") for e in result["errors"])))
        return result["data"]

    def wait(self, seconds: float = 90) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=5):
                    return
            except OSError:
                if time.monotonic() > deadline:
                    raise SetupError(f"Phoenix did not answer at {self.url} within {seconds:.0f} seconds") from None
                time.sleep(2)


# ---- providers -------------------------------------------------------------------


def ensure_provider(px: Phoenix, name: str, base_url: str, api_key: str) -> str:
    """Create the provider, or point the existing one at this URL and key. Returns its id."""
    config = {
        "openai": {
            "openaiAuthenticationMethod": {"apiKey": api_key},
            "openaiClientKwargs": {"baseUrl": base_url},
            # Phoenix's default is the Responses API, which neither our agent nor this setup uses.
            "openaiApiType": "CHAT_COMPLETIONS",
        }
    }
    found = px.gql("{ generativeModelCustomProviders(first: 100) { edges { node { id name } } } }")
    existing = {e["node"]["name"]: e["node"]["id"] for e in found["generativeModelCustomProviders"]["edges"]}
    if name in existing:
        px.gql(
            "mutation($input: PatchGenerativeModelCustomProviderMutationInput!) { patchGenerativeModelCustomProvider(input: $input) { provider { id } } }",
            {"input": {"id": existing[name], "clientConfig": config}},
        )
        return existing[name]
    created = px.gql(
        "mutation($input: CreateGenerativeModelCustomProviderMutationInput!) { createGenerativeModelCustomProvider(input: $input) { provider { id } } }",
        {"input": {"name": name, "provider": "openai", "clientConfig": config}},
    )
    return created["createGenerativeModelCustomProvider"]["provider"]["id"]


# ---- prompts ---------------------------------------------------------------------

Messages = list[tuple[str, str]]  # (role, text)

MESSAGES_FIELDS = "template { ... on PromptChatTemplate { messages { role content { ... on TextContentPart { text { text } } } } } }"


def _version_input(messages: Messages, model: str, provider_id: str, tools: dict[str, Any] | None = None) -> dict[str, Any]:
    version: dict[str, Any] = {
        "templateFormat": "MUSTACHE",
        "template": {"messages": [{"role": role, "content": [{"text": {"text": text}}]} for role, text in messages]},
        "invocationParameters": {"openai": {}},
        "modelProvider": "OPENAI",
        "modelName": model,
        "customProviderId": provider_id,
    }
    if tools:
        version["tools"] = tools
    return version


def _same_version(saved: dict[str, Any], messages: Messages, model: str, provider_id: str) -> bool:
    saved_messages = [(m["role"], "".join(part.get("text", {}).get("text", "") for part in m["content"])) for m in saved["template"]["messages"]]
    return saved_messages == messages and saved["modelName"] == model and (saved.get("customProvider") or {}).get("id") == provider_id


def ensure_prompt(px: Phoenix, provider_id: str) -> tuple[str, str]:
    """Save the agent's prompt in Phoenix. A new version only when it changed: every save is a version.

    The prompt also carries the model (`diagnoster-recorded`), so opening it in
    the Playground picks the agent too. Returns its id and what was done.
    """
    messages: Messages = [("SYSTEM", SYSTEM_PROMPT), ("USER", "{{question}}")]
    version = _version_input(messages, DEFAULT_MODEL_NAME, provider_id)
    found = px.gql(
        "query($name: String!) { prompts(first: 10, filter: {col: name, value: $name}) { edges { node { id name version { modelName customProvider { id } %s } } } } }"
        % MESSAGES_FIELDS,
        {"name": PROMPT},
    )
    saved = next((e["node"] for e in found["prompts"]["edges"] if e["node"]["name"] == PROMPT), None)
    if saved is None:
        description = "The diagnosis agent's system prompt. The copy in agent/src/diagnoster/agent.py is the official one."
        created = px.gql(
            "mutation($input: CreateChatPromptInput!) { createChatPrompt(input: $input) { id } }",
            {"input": {"name": PROMPT, "description": description, "promptVersion": version}},
        )
        return created["createChatPrompt"]["id"], "created"
    if _same_version(saved["version"], messages, DEFAULT_MODEL_NAME, provider_id):
        return saved["id"], "unchanged"
    px.gql(
        "mutation($input: CreateChatPromptVersionInput!) { createChatPromptVersion(input: $input) { id } }",
        {"input": {"promptId": saved["id"], "promptVersion": version}},
    )
    return saved["id"], "new version"


# ---- dataset ---------------------------------------------------------------------


def ensure_dataset(px: Phoenix, examples: list[dict[str, Any]]) -> tuple[str, str]:
    """Make the dataset's rows equal ours: Phoenix adds, changes and removes rows by their id."""
    body = {
        "action": "update",
        "name": dataset.DATASET,
        "description": dataset.DESCRIPTION,
        "inputs": [e["input"] for e in examples],
        "outputs": [e["output"] for e in examples],
        "metadata": [e["metadata"] for e in examples],
        "splits": [e["splits"] for e in examples],
        "example_ids": [e["id"] for e in examples],
    }
    data = px.post("/v1/datasets/upload?sync=true", body)["data"]
    # An upload to an existing dataset keeps its old description.
    px.gql(
        "mutation($input: PatchDatasetInput!) { patchDataset(input: $input) { dataset { id } } }",
        {"input": {"datasetId": data["dataset_id"], "description": dataset.DESCRIPTION}},
    )
    changed = {k: data.get(f"num_{k}_examples", 0) for k in ("created", "updated", "deleted")}
    summary = ", ".join(f"{n} {k}" for k, n in changed.items() if n) or "unchanged"
    return data["dataset_id"], f"{len(examples)} rows ({summary})"


# ---- evaluators ------------------------------------------------------------------


def _categorical(name: str, labels: dict[str, float]) -> dict[str, Any]:
    values = [{"label": label, "score": score} for label, score in labels.items()]
    return {"categorical": {"name": name, "optimizationDirection": "MAXIMIZE", "values": values}}


def _saved_evaluators(px: Phoenix, dataset_id: str) -> dict[str, dict[str, Any]]:
    found = px.gql(
        """query($id: ID!) { node(id: $id) { ... on Dataset { datasetEvaluators(first: 50) { edges { node {
             id name evaluator { ... on CodeEvaluator { sourceCode } ... on LLMEvaluator { promptVersion { modelName customProvider { id } %s } } }
           } } } } } }"""
        % MESSAGES_FIELDS,
        {"id": dataset_id},
    )
    return {e["node"]["name"]: e["node"] for e in found["node"]["datasetEvaluators"]["edges"]}


def _replace(px: Phoenix, saved: dict[str, Any] | None) -> str:
    """Our files are the source, so a changed evaluator is replaced, not edited."""
    if saved is None:
        return "created"
    px.gql(
        "mutation($input: DeleteDatasetEvaluatorsInput!) { deleteDatasetEvaluators(input: $input) { datasetEvaluatorIds } }",
        {"input": {"datasetEvaluatorIds": [saved["id"]], "deleteAssociatedPrompt": True}},
    )
    return "replaced"


def _sandbox_id(px: Phoenix) -> str:
    providers = px.gql("{ sandboxProviders { configs { id name } } }")["sandboxProviders"]
    configs = {c["name"]: c["id"] for p in providers for c in p["configs"]}
    if SANDBOX not in configs:
        raise SetupError(f"Phoenix has no sandbox named {SANDBOX}; it has: {', '.join(sorted(configs)) or 'none'}")
    return configs[SANDBOX]


def ensure_code_evaluator(px: Phoenix, dataset_id: str, name: str, saved: dict[str, Any] | None, sandbox_id: str) -> str:
    description, module = CODE_EVALUATORS[name]
    source = Path(module.__file__).read_text()
    if saved and saved["evaluator"].get("sourceCode") == source:
        return "unchanged"
    done = _replace(px, saved)
    px.gql(
        "mutation($input: CreateDatasetCodeEvaluatorInput!) { createDatasetCodeEvaluator(input: $input) { evaluator { id } } }",
        {
            "input": {
                "datasetId": dataset_id,
                "name": name,
                "description": description,
                "source": {"definition": {"sourceCode": source, "language": "PYTHON", "sandboxConfigId": sandbox_id}},
                "inputMapping": {"literalMapping": {}, "pathMapping": {"output": "output", "reference": "reference"}},
                "outputConfigs": [_categorical(name, PASS_FAIL)],
            }
        },
    )
    return done


def ensure_action_evaluator(px: Phoenix, dataset_id: str, saved: dict[str, Any] | None, provider_id: str, model: str) -> str:
    messages: Messages = [("SYSTEM", action.SYSTEM), ("USER", action.USER)]
    if saved and "promptVersion" in saved["evaluator"] and _same_version(saved["evaluator"]["promptVersion"], messages, model, provider_id):
        return "unchanged"
    done = _replace(px, saved)
    # Phoenix asks the judge for its label through one forced tool call, named like the evaluator.
    tool = {
        "name": "action",
        "description": action.DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": list(action.LABELS), "description": "action"},
                "explanation": {"type": "string", "description": "One or two sentences: which safe or unsafe action it matched, or why none."},
            },
            "required": ["label", "explanation"],
        },
    }
    tools = {"tools": [{"function": tool}], "toolChoice": {"functionName": "action"}}
    px.gql(
        "mutation($input: CreateDatasetLLMEvaluatorInput!) { createDatasetLlmEvaluator(input: $input) { evaluator { id } } }",
        {
            "input": {
                "datasetId": dataset_id,
                "name": "action",
                "description": action.DESCRIPTION,
                "promptVersion": _version_input(messages, model, provider_id, tools),
                "outputConfigs": [_categorical("action", action.LABELS)],
                "inputMapping": {"literalMapping": {}, "pathMapping": action.PATHS},
            }
        },
    )
    return done


# ---- the whole setup ---------------------------------------------------------------


def run(px: Phoenix, agent_url: str, api_key: str, judge_model: str, browser_url: str, say=print) -> None:
    px.wait()
    recorded = ensure_provider(px, RECORDED, f"{agent_url}/recorded/v1", "not-needed")
    ensure_provider(px, LIVE, f"{agent_url}/live/v1", "not-needed")
    say(f"models     {RECORDED}, {LIVE} -> {agent_url}")
    prompt_id, done = ensure_prompt(px, recorded)
    say(f"prompt     {PROMPT}: {done}")
    dataset_id, rows = ensure_dataset(px, dataset.build_examples(load_all()))
    say(f"dataset    {dataset.DATASET}: {rows}")
    saved = _saved_evaluators(px, dataset_id)
    sandbox_id = _sandbox_id(px)
    for name in CODE_EVALUATORS:
        say(f"evaluator  {name}: {ensure_code_evaluator(px, dataset_id, name, saved.get(name), sandbox_id)}")
    if api_key:
        judge = ensure_provider(px, JUDGE_PROVIDER, OPENROUTER_URL, api_key)
        say(f"evaluator  action: {ensure_action_evaluator(px, dataset_id, saved.get('action'), judge, judge_model)} (judge {judge_model})")
    else:
        say("evaluator  action: SKIPPED. Phoenix's judge needs OPENROUTER_API_KEY; set it in .env and run this again.")
    # The Playground with our prompt (and so our agent as the model) and the dataset with its evaluators.
    query = urllib.parse.urlencode({"promptId": prompt_id, "datasetId": dataset_id})
    say(f"\nRun the evals (press Run): {browser_url}/playground?{query}")


def main() -> int:
    settings = load_settings()  # also loads .env, when run from a checkout
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    # Setup talks to Phoenix at one address; Phoenix calls the agent at another (its Compose service name).
    px = Phoenix(os.environ.get("PHOENIX_INTERNAL_URL") or settings.phoenix_url, secrets=(api_key,))
    agent_url = os.environ.get("AGENT_URL_FOR_PHOENIX", "http://agent:8000").rstrip("/")
    try:
        run(px, agent_url, api_key, os.environ.get("JUDGE_MODEL") or action.DEFAULT_JUDGE_MODEL, settings.phoenix_url)
    except SetupError as e:
        print(f"phoenix-setup failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
