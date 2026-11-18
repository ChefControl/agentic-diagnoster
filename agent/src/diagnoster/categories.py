"""The root-cause categories the agent chooses from.

scenarios/categories.yaml is the single source of truth: the graders read the
same file, so the agent and the answer keys can't drift apart. The agent's
container copies only this one file, never the answer keys next to it.
"""

import os
from enum import StrEnum
from pathlib import Path

import yaml

# agent/src/diagnoster/categories.py -> the repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILE = _REPO_ROOT / "scenarios" / "categories.yaml"


def load_categories(path: Path | str | None = None) -> dict[str, str]:
    """Category id -> description, in file order."""
    path = Path(path or os.environ.get("CATEGORIES_FILE") or DEFAULT_FILE)
    data = yaml.safe_load(path.read_text())
    return {c["id"]: c["description"] for c in data["categories"]}


CATEGORIES = load_categories()

# Built from the file at import, so a new category in the YAML needs no code change.
Category = StrEnum("Category", {cid: cid for cid in CATEGORIES})
