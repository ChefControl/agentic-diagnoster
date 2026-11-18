"""phoenix-setup against a fake Phoenix: what it creates, what a second run leaves alone, and that no key is ever sent."""

import json
import re

import pytest

from diagnoster.agent import SYSTEM_PROMPT
from diagnoster.evals import dataset, setup
from diagnoster.evals.phoenix import action, diagnosis


class FakePhoenix(setup.Phoenix):
    """Remembers what was created, answers the setup's queries from that, and records every request."""

    def __init__(self):
        super().__init__("http://phoenix.test")
        self.providers: dict[str, dict] = {}
        self.prompt: dict | None = None  # {"id", "versions": [version input]}
        self.evaluators: dict[str, dict] = {}  # name -> {"id", "source" | "version"}
        self.rows: list[dict] = []
        self.mutations: list[str] = []
        self.sent: list[str] = []  # every request body, to check no key is ever sent
        self._ids = 0

    def _id(self) -> str:
        self._ids += 1
        return f"id{self._ids}"

    def wait(self, seconds: float = 90) -> None:
        pass

    def post(self, path, body):
        self.sent.append(json.dumps(body))
        assert path == "/v1/datasets/upload?sync=true"
        old = {r["id"]: r for r in self.rows}
        new = [dict(id=i, input=a, output=b) for i, a, b in zip(body["example_ids"], body["inputs"], body["outputs"], strict=True)]
        created = sum(r["id"] not in old for r in new)
        updated = sum(r["id"] in old and old[r["id"]] != r for r in new)
        deleted = len(set(old) - {r["id"] for r in new})
        self.rows = new
        counts = {"num_created_examples": created, "num_updated_examples": updated, "num_deleted_examples": deleted}
        return {"data": {"dataset_id": "dataset1", **counts}}

    @staticmethod
    def _saved_version(version: dict) -> dict:
        """A version input as Phoenix returns it from a query."""
        messages = [{"role": m["role"], "content": [{"text": {"text": p["text"]["text"]}} for p in m["content"]]} for m in version["template"]["messages"]]
        custom = {"id": version["customProviderId"]} if version.get("customProviderId") else None
        return {"modelName": version["modelName"], "modelProvider": version["modelProvider"], "customProvider": custom, "template": {"messages": messages}}

    def gql(self, query, variables=None):
        variables = variables or {}
        self.sent.append(json.dumps(variables))
        name = re.match(r"\s*(\w+)", query.split("{", 1)[1]).group(1)  # the first field asked for
        if query.lstrip().startswith("mutation"):
            self.mutations.append(name)
        data = variables.get("input", {})
        match name:
            case "generativeModelCustomProviders":
                edges = [{"node": {"id": p["id"], "name": n}} for n, p in self.providers.items()]
                return {"generativeModelCustomProviders": {"edges": edges}}
            case "createGenerativeModelCustomProvider":
                self.providers[data["name"]] = {"id": self._id(), "sdk": data["provider"], "config": data["clientConfig"]}
                return {"createGenerativeModelCustomProvider": {"provider": {"id": self.providers[data["name"]]["id"]}}}
            case "patchGenerativeModelCustomProvider":
                provider = next(p for p in self.providers.values() if p["id"] == data["id"])
                provider["config"] = data["clientConfig"]
                return {"patchGenerativeModelCustomProvider": {"provider": {"id": data["id"]}}}
            case "deleteGenerativeModelCustomProvider":
                self.providers = {n: p for n, p in self.providers.items() if p["id"] != data["id"]}
                return {"deleteGenerativeModelCustomProvider": {"id": data["id"]}}
            case "prompts":
                if self.prompt is None:
                    return {"prompts": {"edges": []}}
                latest = self._saved_version(self.prompt["versions"][-1])
                return {"prompts": {"edges": [{"node": {"id": self.prompt["id"], "name": setup.PROMPT, "version": latest}}]}}
            case "createChatPrompt":
                self.prompt = {"id": self._id(), "versions": [data["promptVersion"]]}
                return {"createChatPrompt": {"id": self.prompt["id"]}}
            case "createChatPromptVersion":
                self.prompt["versions"].append(data["promptVersion"])
                return {"createChatPromptVersion": {"id": self.prompt["id"]}}
            case "patchDataset":
                return {"patchDataset": {"dataset": {"id": data["datasetId"]}}}
            case "node":
                edges = []
                for evaluator_name, e in self.evaluators.items():
                    inner = {"sourceCode": e["source"]} if "source" in e else {"promptVersion": self._saved_version(e["version"])}
                    mapping = e["input"]["inputMapping"]
                    edges.append({"node": {"id": e["id"], "name": evaluator_name, "inputMapping": mapping, "evaluator": inner}})
                return {"node": {"datasetEvaluators": {"edges": edges}}}
            case "sandboxProviders":
                return {"sandboxProviders": [{"configs": [{"id": "sandbox1", "name": setup.SANDBOX}]}]}
            case "deleteDatasetEvaluators":
                self.evaluators = {n: e for n, e in self.evaluators.items() if e["id"] not in data["datasetEvaluatorIds"]}
                return {"deleteDatasetEvaluators": {"datasetEvaluatorIds": data["datasetEvaluatorIds"]}}
            case "createDatasetCodeEvaluator":
                self.evaluators[data["name"]] = {"id": self._id(), "source": data["source"]["definition"]["sourceCode"], "input": data}
                return {"createDatasetCodeEvaluator": {"evaluator": {"id": self.evaluators[data["name"]]["id"]}}}
            case "createDatasetLlmEvaluator":
                self.evaluators[data["name"]] = {"id": self._id(), "version": data["promptVersion"], "input": data}
                return {"createDatasetLlmEvaluator": {"evaluator": {"id": self.evaluators[data["name"]]["id"]}}}
        raise AssertionError(f"the fake doesn't know {name}")


BOTH = ["openai", "anthropic"]


def run(px, vendors=BOTH, default_model="openai:gpt-6-luna", judge="from-keys"):
    said = []
    judge_model = setup.judge_for(vendors, None) if judge == "from-keys" else judge
    setup.run(px, "http://agent:8000", vendors, default_model, judge_model, "http://localhost:6006", say=said.append)
    return "\n".join(said)


def test_a_first_run_creates_everything():
    px = FakePhoenix()
    said = run(px)
    assert set(px.providers) == {"diagnoster-openai", "diagnoster-claude"}
    openai_entry, claude_entry = px.providers["diagnoster-openai"], px.providers["diagnoster-claude"]
    assert (openai_entry["sdk"], claude_entry["sdk"]) == ("openai", "anthropic")
    # Each vendor's SDK adds its own path: /chat/completions after /v1, and /v1/messages after the root.
    assert openai_entry["config"]["openai"]["openaiClientKwargs"]["baseUrl"] == "http://agent:8000/v1"
    assert openai_entry["config"]["openai"]["openaiApiType"] == "CHAT_COMPLETIONS"
    assert claude_entry["config"]["anthropic"]["anthropicClientKwargs"]["baseUrl"] == "http://agent:8000"
    assert set(px.evaluators) == {"diagnosis", "evidence", "action"}
    assert [r["id"] for r in px.rows] == [e["id"] for e in dataset.build_examples(setup.load_all())]
    assert f"Run the evals (press Run): http://localhost:6006/playground?promptId={px.prompt['id']}&datasetId=dataset1" in said


def test_no_key_is_ever_sent_to_phoenix(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-not-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    px = FakePhoenix()
    run(px)
    assert not any("sk-test-openai-not-real" in sent or "sk-ant-test-not-real" in sent for sent in px.sent)


def test_the_saved_prompt_is_the_agents_prompt_on_the_default_model():
    px = FakePhoenix()
    run(px)
    version = px.prompt["versions"][0]
    messages = version["template"]["messages"]
    assert [(m["role"], m["content"][0]["text"]["text"]) for m in messages] == [("SYSTEM", SYSTEM_PROMPT), ("USER", "{{question}}")]
    assert (version["modelProvider"], version["modelName"]) == ("OPENAI", "gpt-6-luna")
    assert version["customProviderId"] == px.providers["diagnoster-openai"]["id"]


def test_a_claude_default_opens_on_the_claude_entry():
    px = FakePhoenix()
    run(px, vendors=["anthropic"], default_model="anthropic:claude-haiku-5-5")
    version = px.prompt["versions"][0]
    assert set(px.providers) == {"diagnoster-claude"}
    assert (version["modelProvider"], version["modelName"]) == ("ANTHROPIC", "claude-haiku-5-5")
    assert version["invocationParameters"]["anthropic"]["maxTokens"] > 0  # Anthropic's API requires it


def test_the_code_evaluators_are_our_files_as_they_are():
    px = FakePhoenix()
    run(px)
    created = px.evaluators["diagnosis"]["input"]
    assert created["source"]["definition"] == {"sourceCode": open(diagnosis.__file__).read(), "language": "PYTHON", "sandboxConfigId": "sandbox1"}
    assert created["inputMapping"]["pathMapping"] == {"output": "output", "reference": "reference"}


@pytest.mark.parametrize(
    ("vendors", "provider", "model"),
    [(BOTH, "ANTHROPIC", "claude-sonnet-5"), (["anthropic"], "ANTHROPIC", "claude-sonnet-5"), (["openai"], "OPENAI", "gpt-6.1-sol")],
)
def test_the_judge_runs_on_phoenixs_own_provider_for_a_key_that_is_set(vendors, provider, model):
    px = FakePhoenix()
    run(px, vendors=vendors, default_model=f"{vendors[0]}:x")
    version = px.evaluators["action"]["input"]["promptVersion"]
    assert (version["modelProvider"], version["modelName"]) == (provider, model)
    assert "customProviderId" not in version  # Phoenix's built-in provider, with the key from .env


def test_the_judge_is_asked_for_one_label_through_a_forced_tool_call():
    px = FakePhoenix()
    run(px)
    created = px.evaluators["action"]["input"]
    tools = created["promptVersion"]["tools"]
    assert tools["toolChoice"] == {"functionName": "action"}
    assert tools["tools"][0]["function"]["parameters"]["properties"]["label"]["enum"] == list(action.LABELS)
    assert created["inputMapping"]["pathMapping"] == action.PATHS


def test_a_second_run_changes_nothing():
    px = FakePhoenix()
    run(px)
    px.mutations.clear()
    said = run(px)
    # Agent entries are re-pointed every time (cheap); nothing else is written.
    assert set(px.mutations) <= {"patchGenerativeModelCustomProvider", "patchDataset"}
    assert len(px.prompt["versions"]) == 1
    assert "8 rows (unchanged)" in said
    assert said.count(": unchanged") == 4  # the prompt and the three evaluators


def test_a_changed_evaluator_file_prompt_or_judge_is_replaced():
    px = FakePhoenix()
    run(px)
    old_id = px.evaluators["evidence"]["id"]
    px.evaluators["evidence"]["source"] = "an older version of the file"
    px.prompt["versions"][-1]["template"]["messages"][0]["content"][0]["text"]["text"] = "an older prompt"
    said = run(px, judge="openai:gpt-6.1-sol")
    assert "evaluator  evidence: replaced" in said and px.evaluators["evidence"]["id"] != old_id
    assert "prompt     diagnoster-system: new version" in said and len(px.prompt["versions"]) == 2
    assert "evaluator  action: replaced (judge openai:gpt-6.1-sol" in said


def test_entries_from_an_earlier_setup_or_a_removed_key_are_removed():
    px = FakePhoenix()
    for old in ("diagnoster-recorded", "diagnoster-live", "openrouter"):
        px.providers[old] = {"id": f"old-{old}", "sdk": "openai", "config": {}}
    run(px)
    assert set(px.providers) == {"diagnoster-openai", "diagnoster-claude"}
    run(px, vendors=["openai"], default_model="openai:gpt-6-luna")
    assert set(px.providers) == {"diagnoster-openai"}


def test_without_any_key_only_the_dataset_and_code_evaluators_are_set_up():
    px = FakePhoenix()
    said = run(px, vendors=[], judge=None)
    assert px.providers == {} and px.prompt is None
    assert set(px.evaluators) == {"diagnosis", "evidence"}
    assert "models     NONE. Set OPENAI_API_KEY or ANTHROPIC_API_KEY" in said
    assert "evaluator  action: SKIPPED" in said and "Run the evals" not in said


def test_a_judge_model_without_a_known_vendor_is_skipped():
    said = run(FakePhoenix(), judge="openrouter:openai/gpt-6.1-terra")
    assert "action: SKIPPED. JUDGE_MODEL 'openrouter:openai/gpt-6.1-terra' is not openai:… or anthropic:…" in said


def test_graphql_errors_are_reported():
    px = setup.Phoenix("http://phoenix.test")
    px.post = lambda path, body: {"errors": [{"message": "something broke"}]}
    with pytest.raises(setup.SetupError, match="something broke"):
        px.gql("{ x }")


def test_the_dataset_rows_are_valid_json():
    # The upload sends them as JSON; a stray non-JSON value (an enum, a Path) would fail only against the real Phoenix.
    for example in dataset.build_examples(setup.load_all()):
        assert json.loads(json.dumps(example)) == example


def test_a_judge_reading_from_old_fields_is_replaced():
    # Its prompt is the same, but the reference moved: the judge must read the new paths.
    px = FakePhoenix()
    run(px)
    px.evaluators["action"]["input"]["inputMapping"] = {"literalMapping": {}, "pathMapping": {"fault": "reference.why"}}
    said = run(px)
    assert "evaluator  action: replaced" in said
    assert px.evaluators["action"]["input"]["inputMapping"]["pathMapping"] == action.PATHS
