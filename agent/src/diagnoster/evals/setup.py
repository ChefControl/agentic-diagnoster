"""`phoenix-setup`: put everything the evals need into the local Phoenix.

After it runs, an eval is a few clicks in Phoenix's Playground: open the
agent's prompt with the dataset (our UI links there), press Run. It creates,
by name:

- the agent in the model picker, once per vendor whose key is set:
  `diagnoster-openai` lists OpenAI's models and `diagnoster-claude` lists
  Claude models; the model you pick is the model the agent runs with;
- the prompt `diagnoster-system`: the agent's prompt, to edit and compare in Phoenix;
- the dataset `diagnoster-scenarios`, built from scenarios/*/expected.yaml;
- three evaluators on that dataset: `diagnosis` and `evidence` (our Python
  files, run in Phoenix's sandbox) and `action` (an LLM judge on one of
  Phoenix's built-in providers).

The keys themselves never pass through here: the agent and Phoenix each read
them from .env. This only looks at which ones are set.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent import SYSTEM_PROMPT
from ..config import load_settings
from . import dataset
from .answer_key import load_all
from .phoenix import action, diagnosis, evidence

PROMPT = "diagnoster-system"
SANDBOX = "default-wasm-python"
PASS_FAIL = {"pass": 1.0, "fail": 0.0}
CODE_EVALUATORS = {
    "diagnosis": ("Did the agent name the expected root cause: category and resource?", diagnosis),
    "evidence": ("Is every required fact backed by a tool call the agent cited?", evidence),
}
# Entries earlier versions of this setup made; removed when found.
RETIRED = ("diagnoster-recorded", "diagnoster-live", "openrouter")


@dataclass(frozen=True)
class Vendor:
    key: str  # PydanticAI's provider name, the prefix in "openai:gpt-6-luna"
    env: str  # the API key's variable
    phoenix: str  # Phoenix's provider key
    entry: str  # our agent's name in Phoenix's model picker
    path: str  # where the vendor's SDK finds our agent, under its base URL

    def invocation(self) -> dict[str, Any]:
        # Anthropic's API requires max_tokens; the agent ignores it.
        return {"anthropic": {"maxTokens": 4096}} if self.key == "anthropic" else {"openai": {}}


VENDORS = {
    "openai": Vendor("openai", "OPENAI_API_KEY", "OPENAI", "diagnoster-openai", "/v1"),
    "anthropic": Vendor("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC", "diagnoster-claude", ""),
}


def judge_for(vendors: list[str], override: str | None) -> str | None:
    """`provider:model` for the judge: JUDGE_MODEL, else the default for the keys that are set."""
    if override:
        return override
    for key in ("anthropic", "openai"):
        if key in vendors:
            return f"{key}:{action.DEFAULT_JUDGE_MODELS[key]}"
    return None


class SetupError(Exception):
    pass


class Phoenix:
    """The few HTTP calls the setup makes. Standard library only."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.url}{path}", json.dumps(body).encode(), {"content-type": "application/json", "accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise SetupError(f"Phoenix answered {e.code} on {path}: {e.read().decode(errors='replace')[:300]}") from None

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.post("/graphql", {"query": query, "variables": variables or {}})
        if result.get("errors"):
            raise SetupError("; ".join(e.get("message", "?") for e in result["errors"]))
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


# ---- the agent in the model picker ---------------------------------------------------


def _providers(px: Phoenix) -> dict[str, str]:
    found = px.gql("{ generativeModelCustomProviders(first: 100) { edges { node { id name } } } }")
    return {e["node"]["name"]: e["node"]["id"] for e in found["generativeModelCustomProviders"]["edges"]}


def ensure_agent_entry(px: Phoenix, vendor: Vendor, agent_url: str, existing: dict[str, str]) -> str:
    """Our agent as a custom provider speaking this vendor's format. Returns its id."""
    base_url = f"{agent_url}{vendor.path}"
    # The agent needs no key from Phoenix; the SDK just requires one.
    if vendor.key == "openai":
        config = {
            "openai": {
                "openaiAuthenticationMethod": {"apiKey": "not-needed"},
                "openaiClientKwargs": {"baseUrl": base_url},
                # Phoenix's default is the Responses API, which the agent doesn't serve.
                "openaiApiType": "CHAT_COMPLETIONS",
            }
        }
    else:
        config = {"anthropic": {"anthropicAuthenticationMethod": {"apiKey": "not-needed"}, "anthropicClientKwargs": {"baseUrl": base_url}}}
    if vendor.entry in existing:
        px.gql(
            "mutation($input: PatchGenerativeModelCustomProviderMutationInput!) { patchGenerativeModelCustomProvider(input: $input) { provider { id } } }",
            {"input": {"id": existing[vendor.entry], "clientConfig": config}},
        )
        return existing[vendor.entry]
    created = px.gql(
        "mutation($input: CreateGenerativeModelCustomProviderMutationInput!) { createGenerativeModelCustomProvider(input: $input) { provider { id } } }",
        {"input": {"name": vendor.entry, "provider": vendor.key, "clientConfig": config}},
    )
    return created["createGenerativeModelCustomProvider"]["provider"]["id"]


def remove_provider(px: Phoenix, provider_id: str) -> None:
    px.gql(
        "mutation($input: DeleteGenerativeModelCustomProviderMutationInput!) { deleteGenerativeModelCustomProvider(input: $input) { id } }",
        {"input": {"id": provider_id}},
    )


# ---- prompts ---------------------------------------------------------------------

Messages = list[tuple[str, str]]  # (role, text)

VERSION_FIELDS = (
    "modelName modelProvider customProvider { id } "
    "template { ... on PromptChatTemplate { messages { role content { ... on TextContentPart { text { text } } } } } }"
)


@dataclass(frozen=True)
class Target:
    """Where a prompt runs: a vendor, a model name there, and our agent's entry (None for Phoenix's own provider)."""

    vendor: Vendor
    model: str
    provider_id: str | None = None


def _version_input(messages: Messages, target: Target, tools: dict[str, Any] | None = None) -> dict[str, Any]:
    version: dict[str, Any] = {
        "templateFormat": "MUSTACHE",
        "template": {"messages": [{"role": role, "content": [{"text": {"text": text}}]} for role, text in messages]},
        "invocationParameters": target.vendor.invocation(),
        "modelProvider": target.vendor.phoenix,
        "modelName": target.model,
    }
    if target.provider_id:
        version["customProviderId"] = target.provider_id
    if tools:
        version["tools"] = tools
    return version


def _same_version(saved: dict[str, Any], messages: Messages, target: Target) -> bool:
    saved_messages = [(m["role"], "".join(part.get("text", {}).get("text", "") for part in m["content"])) for m in saved["template"]["messages"]]
    return (
        saved_messages == messages
        and saved["modelName"] == target.model
        and saved["modelProvider"] == target.vendor.phoenix
        and (saved.get("customProvider") or {}).get("id") == target.provider_id
    )


def ensure_prompt(px: Phoenix, target: Target) -> tuple[str, str]:
    """Save the agent's prompt in Phoenix. A new version only when it changed: every save is a version.

    The prompt also carries the model (our agent's entry and the default
    model's name), so opening it in the Playground picks the agent too.
    Returns its id and what was done.
    """
    messages: Messages = [("SYSTEM", SYSTEM_PROMPT), ("USER", "{{question}}")]
    version = _version_input(messages, target)
    found = px.gql(
        "query($name: String!) { prompts(first: 10, filter: {col: name, value: $name}) { edges { node { id name version { %s } } } } }"
        % VERSION_FIELDS,
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
    if _same_version(saved["version"], messages, target):
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
             id name inputMapping { pathMapping } evaluator { ... on CodeEvaluator { sourceCode } ... on LLMEvaluator { promptVersion { %s } } }
           } } } } } }"""
        % VERSION_FIELDS,
        {"id": dataset_id},
    )
    return {e["node"]["name"]: e["node"] for e in found["node"]["datasetEvaluators"]["edges"]}


def _remove_evaluator(px: Phoenix, saved: dict[str, Any]) -> None:
    px.gql(
        "mutation($input: DeleteDatasetEvaluatorsInput!) { deleteDatasetEvaluators(input: $input) { datasetEvaluatorIds } }",
        {"input": {"datasetEvaluatorIds": [saved["id"]], "deleteAssociatedPrompt": True}},
    )


def _replace(px: Phoenix, saved: dict[str, Any] | None) -> str:
    """Our files are the source, so a changed evaluator is replaced, not edited."""
    if saved is None:
        return "created"
    _remove_evaluator(px, saved)
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


def ensure_action_evaluator(px: Phoenix, dataset_id: str, saved: dict[str, Any] | None, judge: Target) -> str:
    messages: Messages = [("SYSTEM", action.SYSTEM), ("USER", action.USER)]
    if (
        saved
        and "promptVersion" in saved["evaluator"]
        and _same_version(saved["evaluator"]["promptVersion"], messages, judge)
        and saved["inputMapping"]["pathMapping"] == action.PATHS  # where it reads the run and the reference
    ):
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
                "promptVersion": _version_input(messages, judge, tools),
                "outputConfigs": [_categorical("action", action.LABELS)],
                "inputMapping": {"literalMapping": {}, "pathMapping": action.PATHS},
            }
        },
    )
    return done


# ---- the whole setup ---------------------------------------------------------------


def run(
    px: Phoenix, agent_url: str, vendors: list[str], default_model: str, judge_model: str | None, browser_url: str, say=print
) -> None:
    px.wait()
    existing = _providers(px)
    for name in RETIRED:
        if name in existing:
            remove_provider(px, existing.pop(name))
            say(f"removed    {name} (from an earlier setup)")

    entries: dict[str, str] = {}
    for key, vendor in VENDORS.items():
        if key in vendors:
            entries[key] = ensure_agent_entry(px, vendor, agent_url, existing)
        elif vendor.entry in existing:  # its key is gone: the agent couldn't run those models
            remove_provider(px, existing[vendor.entry])
    if entries:
        say(f"models     {', '.join(VENDORS[k].entry for k in entries)} -> {agent_url}")
    else:
        say("models     NONE. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env and run this again.")

    prompt_id = None
    if entries:
        # The prompt opens on the agent's default model, under that vendor's entry.
        vendor_key, _, model = default_model.partition(":")
        if vendor_key not in entries:  # e.g. MODEL names a vendor without a key: the agent decides
            vendor_key, model = next(iter(entries)), "default"
        prompt_id, done = ensure_prompt(px, Target(VENDORS[vendor_key], model, entries[vendor_key]))
        say(f"prompt     {PROMPT}: {done} ({VENDORS[vendor_key].entry} / {model})")

    dataset_id, rows = ensure_dataset(px, dataset.build_examples(load_all()))
    say(f"dataset    {dataset.DATASET}: {rows}")
    saved = _saved_evaluators(px, dataset_id)
    sandbox_id = _sandbox_id(px)
    for name in CODE_EVALUATORS:
        say(f"evaluator  {name}: {ensure_code_evaluator(px, dataset_id, name, saved.get(name), sandbox_id)}")
    judge_vendor, _, judge_name = (judge_model or "").partition(":")
    if judge_model and judge_vendor in VENDORS:
        done = ensure_action_evaluator(px, dataset_id, saved.get("action"), Target(VENDORS[judge_vendor], judge_name))
        say(f"evaluator  action: {done} (judge {judge_model}, through Phoenix's own {VENDORS[judge_vendor].phoenix.title().replace('Openai', 'OpenAI')} provider)")
    else:
        if saved.get("action"):
            _remove_evaluator(px, saved["action"])
        reason = f"JUDGE_MODEL {judge_model!r} is not openai:… or anthropic:…" if judge_model else "the judge needs OPENAI_API_KEY or ANTHROPIC_API_KEY"
        say(f"evaluator  action: SKIPPED. {reason}; set it in .env and run this again.")

    if prompt_id:
        # The Playground with our prompt (and so our agent as the model) and the dataset with its evaluators.
        query = urllib.parse.urlencode({"promptId": prompt_id, "datasetId": dataset_id})
        say(f"\nRun the evals (press Run): {browser_url}/playground?{query}")


def main() -> int:
    settings = load_settings()  # also loads .env, when run from a checkout
    vendors = [key for key, vendor in VENDORS.items() if os.environ.get(vendor.env)]
    # Setup talks to Phoenix at one address; Phoenix calls the agent at another (its Compose service name).
    px = Phoenix(os.environ.get("PHOENIX_INTERNAL_URL") or settings.phoenix_url)
    agent_url = os.environ.get("AGENT_URL_FOR_PHOENIX", "http://agent:8000").rstrip("/")
    judge = judge_for(vendors, os.environ.get("JUDGE_MODEL"))
    try:
        run(px, agent_url, vendors, settings.model, judge, settings.phoenix_url)
    except SetupError as e:
        print(f"phoenix-setup failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
