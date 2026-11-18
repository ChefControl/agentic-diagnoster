"""Settings, read from the environment and the repository's .env file.

Real environment variables win over .env, so a container or a one-off
`MODEL=... uv run diagnose` overrides the file.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_MODEL = "openrouter:openai/gpt-6-luna"
DEFAULT_NAMESPACES = ("shop", "payments", "catalog", "inventory")


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
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
    run_timeout_s: float = 180


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    env = os.environ.get
    namespaces = tuple(n.strip() for n in env("AGENT_NAMESPACES", ",".join(DEFAULT_NAMESPACES)).split(",") if n.strip())
    defaults = Settings()
    return Settings(
        model=env("MODEL", defaults.model),
        reader_kubeconfig=Path(env("READER_KUBECONFIG", str(defaults.reader_kubeconfig))),
        remediator_kubeconfig=Path(env("REMEDIATOR_KUBECONFIG", str(defaults.remediator_kubeconfig))),
        namespaces=namespaces,
        phoenix_collector_endpoint=env("PHOENIX_COLLECTOR_ENDPOINT", defaults.phoenix_collector_endpoint),
        phoenix_url=env("PHOENIX_URL", defaults.phoenix_url).rstrip("/"),
        request_limit=int(env("REQUEST_LIMIT", defaults.request_limit)),
        tool_calls_limit=int(env("TOOL_CALLS_LIMIT", defaults.tool_calls_limit)),
        run_timeout_s=float(env("RUN_TIMEOUT_S", defaults.run_timeout_s)),
    )
