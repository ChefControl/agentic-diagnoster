"""Settings, read from the environment and the repository's .env file.

Real environment variables win over .env, so a container or a one-off
`MODEL=...` set for one run overrides the file.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]

# The default model is chosen by which key is set: OpenAI's when there is one,
# else Anthropic's. MODEL overrides both.
OPENAI_DEFAULT = "openai:gpt-6-luna"
ANTHROPIC_DEFAULT = "anthropic:claude-haiku-5-5"
DEFAULT_NAMESPACES = ("shop", "payments", "catalog", "inventory")

ClusterMode = Literal["live", "recorded"]
CLUSTER_MODES: tuple[ClusterMode, ...] = ("live", "recorded")


def default_model() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return OPENAI_DEFAULT
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ANTHROPIC_DEFAULT
    return OPENAI_DEFAULT  # no key at all: each run then fails with a message naming the key


@dataclass(frozen=True)
class Settings:
    model: str = OPENAI_DEFAULT
    # live reads the real cluster as the reader; recorded replays the saved
    # snapshot of the four scenarios and needs no cluster.
    cluster_mode: ClusterMode = "live"
    reader_kubeconfig: Path = REPO_ROOT / ".kube" / "reader.kubeconfig"
    # Loaded only by the remediation workflow, after a person approves the fix.
    remediator_kubeconfig: Path = REPO_ROOT / ".kube" / "remediator.kubeconfig"
    # The only namespaces the tools accept. The reader's RBAC allows these too,
    # so the list here only saves the model a forbidden call.
    namespaces: tuple[str, ...] = DEFAULT_NAMESPACES
    phoenix_collector_endpoint: str = "http://localhost:6006/v1/traces"
    phoenix_url: str = "http://localhost:6006"
    request_limit: int = 10
    tool_calls_limit: int = 15
    # Input plus output tokens over the whole run, approval resume included.
    # Real runs used 4k-40k (median ~13k), so this stops only a runaway.
    total_tokens_limit: int = 100_000
    run_timeout_s: float = 180


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    env = os.environ.get
    namespaces = tuple(n.strip() for n in env("AGENT_NAMESPACES", ",".join(DEFAULT_NAMESPACES)).split(",") if n.strip())
    cluster_mode = env("CLUSTER_MODE") or "live"
    if cluster_mode not in CLUSTER_MODES:
        raise ValueError(f"CLUSTER_MODE is {cluster_mode!r}; use live or recorded")
    defaults = Settings()
    return Settings(
        model=env("MODEL") or default_model(),
        cluster_mode=cluster_mode,
        reader_kubeconfig=Path(env("READER_KUBECONFIG", str(defaults.reader_kubeconfig))),
        remediator_kubeconfig=Path(env("REMEDIATOR_KUBECONFIG", str(defaults.remediator_kubeconfig))),
        namespaces=namespaces,
        phoenix_collector_endpoint=env("PHOENIX_COLLECTOR_ENDPOINT", defaults.phoenix_collector_endpoint),
        phoenix_url=env("PHOENIX_URL", defaults.phoenix_url).rstrip("/"),
        request_limit=int(env("REQUEST_LIMIT", defaults.request_limit)),
        tool_calls_limit=int(env("TOOL_CALLS_LIMIT", defaults.tool_calls_limit)),
        total_tokens_limit=int(env("TOTAL_TOKENS_LIMIT", defaults.total_tokens_limit)),
        run_timeout_s=float(env("RUN_TIMEOUT_S", defaults.run_timeout_s)),
    )
