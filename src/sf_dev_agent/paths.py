"""Path resolution helpers — centralizes workspace and project-root lookup."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Path to the repo root (the directory containing pyproject.toml).

    Walks up from this file's location: src/sf_dev_agent/paths.py -> repo root.
    """
    return Path(__file__).resolve().parent.parent.parent


def agent_workspace() -> Path:
    """Path to the SFDX workspace where the agent reads/writes metadata.

    Resolution:
      1. AGENT_WORKSPACE env var (if set and non-empty)
      2. <repo>/workspace (the SFDX project that ships with this repo)
    """
    env = os.environ.get("AGENT_WORKSPACE", "").strip()
    return Path(env) if env else repo_root() / "workspace"
