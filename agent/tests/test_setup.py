"""phoenix-setup against a fake Phoenix: what it creates, what a second run leaves alone, and that the key never leaks."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from diagnoster.agent import SYSTEM_PROMPT
from diagnoster.evals import dataset, setup
from diagnoster.evals.phoenix import action, diagnosis

KEY = "sk-or-test-not-a-real-key"


class FakePhoenix(setup.Phoenix):
    """Remembers what was created, answers the setup's queries from that, and records every mutation."""

    def __init__(self):
        super().__init__("http://phoenix.test", secrets=(KEY,))
        self.providers: dict[str, dict] = {}
        self.prompt: dict | None = None  # {"id", "versions": [version input]}
        self.evaluators: dict[str, dict] = {}  # name -> {"id", "source" | "version"}
        self.rows: list[dict] = []
        self.mutations: list[str] = []
        self._ids = 0

    def _id(self) -> str:
        self._ids += 1
        return f"id{self._ids}"

    def wait(self, seconds: float = 90) -> None:
        pass

    def post(self, path, body):
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
        return {"modelName": version["modelName"], "customProvider": {"id": version["customProviderId"]}, "template": {"messages": messages}}

    def gql(self, query, variables=None):
        variables = variables or {}
        name = re.match(r"\s*(\w+)", query.split("{", 1)[1]).group(1)  # the first field asked for
        if query.lstrip().startswith("mutation"):
            self.mutations.append(name)
        data = variables.get("input", {})
        match name:
            case "generativeModelCustomProviders":
                edges = [{"node": {"id": p["id"], "name": n}} for n, p in self.providers.items()]
                return {"generativeModelCustomProviders": {"edges": edges}}
            case "createGenerativeModelCustomProvider":
                self.providers[data["name"]] = {"id": self._id(), "config": data["clientConfig"]}
                return {"createGenerativeModelCustomProvider": {"provider": {"id": self.providers[data["name"]]["id"]}}}
            case "patchGenerativeModelCustomProvider":
                provider = next(p for p in self.providers.values() if p["id"] == data["id"])
                provider["config"] = data["clientConfig"]
                return {"patchGenerativeModelCustomProvider": {"provider": {"id": data["id"]}}}
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
                    edges.append({"node": {"id": e["id"], "name": evaluator_name, "evaluator": inner}})
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


def run(px, key=KEY):
    said = []
    setup.run(px, "http://agent:8000", key, "openai/gpt-6.1-terra", "http://localhost:6006", say=said.append)
    return "\n".join(said)


def test_a_first_run_creates_everything():
    px = FakePhoenix()
    said = run(px)
    assert set(px.providers) == {"diagnoster-recorded", "diagnoster-live", "openrouter"}
    assert px.providers["diagnoster-recorded"]["config"]["openai"]["openaiClientKwargs"]["baseUrl"] == "http://agent:8000/recorded/v1"
    assert px.providers["diagnoster-live"]["config"]["openai"]["openaiClientKwargs"]["baseUrl"] == "http://agent:8000/live/v1"
    # Phoenix's default API type would call /v1/responses, which the agent doesn't serve.
    assert {p["config"]["openai"]["openaiApiType"] for p in px.providers.values()} == {"CHAT_COMPLETIONS"}
    assert px.providers["openrouter"]["config"]["openai"]["openaiAuthenticationMethod"]["apiKey"] == KEY
    assert set(px.evaluators) == {"diagnosis", "evidence", "action"}
    assert [r["id"] for r in px.rows] == [e["id"] for e in dataset.build_examples(setup.load_all())]
    assert f"Run the evals (press Run): http://localhost:6006/playground?promptId={px.prompt['id']}&datasetId=dataset1" in said


def test_the_saved_prompt_is_the_agents_prompt_with_the_question_slot():
    px = FakePhoenix()
    run(px)
    messages = px.prompt["versions"][0]["template"]["messages"]
    assert [(m["role"], m["content"][0]["text"]["text"]) for m in messages] == [("SYSTEM", SYSTEM_PROMPT), ("USER", "{{question}}")]
    assert px.prompt["versions"][0]["customProviderId"] == px.providers["diagnoster-recorded"]["id"]


def test_the_code_evaluators_are_our_files_as_they_are():
    px = FakePhoenix()
    run(px)
    created = px.evaluators["diagnosis"]["input"]
    assert created["source"]["definition"] == {"sourceCode": open(diagnosis.__file__).read(), "language": "PYTHON", "sandboxConfigId": "sandbox1"}
    assert created["inputMapping"]["pathMapping"] == {"output": "output", "reference": "reference"}


def test_the_judge_is_asked_for_one_label_through_a_forced_tool_call():
    px = FakePhoenix()
    run(px)
    created = px.evaluators["action"]["input"]
    tools = created["promptVersion"]["tools"]
    assert tools["toolChoice"] == {"functionName": "action"}
    assert tools["tools"][0]["function"]["parameters"]["properties"]["label"]["enum"] == list(action.LABELS)
    assert created["inputMapping"]["pathMapping"] == action.PATHS
    assert created["promptVersion"]["customProviderId"] == px.providers["openrouter"]["id"]


def test_a_second_run_changes_nothing():
    px = FakePhoenix()
    run(px)
    px.mutations.clear()
    said = run(px)
    # Providers are re-pointed every time (cheap, and picks up a new key); nothing else is written.
    assert set(px.mutations) <= {"patchGenerativeModelCustomProvider", "patchDataset"}
    assert len(px.prompt["versions"]) == 1
    assert "8 rows (unchanged)" in said
    assert said.count(": unchanged") == 4  # the prompt and the three evaluators


def test_a_changed_evaluator_file_or_prompt_is_replaced(monkeypatch):
    px = FakePhoenix()
    run(px)
    old_id = px.evaluators["evidence"]["id"]
    px.evaluators["evidence"]["source"] = "an older version of the file"
    px.prompt["versions"][-1]["template"]["messages"][0]["content"][0]["text"]["text"] = "an older prompt"
    said = run(px)
    assert "evaluator  evidence: replaced" in said and px.evaluators["evidence"]["id"] != old_id
    assert "prompt     diagnoster-system: new version" in said and len(px.prompt["versions"]) == 2


def test_without_a_key_everything_but_the_judge_is_set_up():
    px = FakePhoenix()
    said = run(px, key="")
    assert "openrouter" not in px.providers
    assert set(px.evaluators) == {"diagnosis", "evidence"}
    assert "evaluator  action: SKIPPED" in said and "OPENROUTER_API_KEY" in said


def test_a_phoenix_error_never_carries_the_key():
    # A stand-in Phoenix that echoes the request back in its error, key included.
    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        px = setup.Phoenix(f"http://127.0.0.1:{server.server_port}", secrets=(KEY,))
        with pytest.raises(setup.SetupError) as error:
            px.gql("mutation { x }", {"apiKey": KEY})
        assert KEY not in str(error.value) and "<redacted>" in str(error.value)
    finally:
        server.shutdown()


def test_graphql_errors_are_reported_without_the_key():
    px = setup.Phoenix("http://phoenix.test", secrets=(KEY,))
    px.post = lambda path, body: {"errors": [{"message": f"bad apiKey {KEY}"}]}
    with pytest.raises(setup.SetupError, match="bad apiKey <redacted>"):
        px.gql("{ x }")


def test_the_dataset_rows_are_valid_json():
    # The upload sends them as JSON; a stray non-JSON value (an enum, a Path) would fail only against the real Phoenix.
    for example in dataset.build_examples(setup.load_all()):
        assert json.loads(json.dumps(example)) == example
