"""Read-only access to the cluster, as the diagnoster-reader service account.

The Python client talks to the API server directly: no kubectl, no
subprocess. What the reader may do is decided by its RBAC, not by this code.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kubernetes import client, config


@dataclass
class Cluster:
    core: Any  # CoreV1Api
    apps: Any  # AppsV1Api
    discovery: Any  # DiscoveryV1Api


def connect(kubeconfig: Path) -> Cluster:
    """Load the kubeconfig fresh for each run, so a re-issued token is picked up."""
    if not kubeconfig.is_file():
        raise FileNotFoundError(f"reader kubeconfig not found at {kubeconfig}; run ./cluster.sh --up")
    api = config.new_client_from_config(config_file=str(kubeconfig))
    return Cluster(core=client.CoreV1Api(api), apps=client.AppsV1Api(api), discovery=client.DiscoveryV1Api(api))
