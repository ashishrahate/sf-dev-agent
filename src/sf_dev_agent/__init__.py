"""Salesforce Developer Agent — An autonomous AI agent for Salesforce platform development."""

import os
from pathlib import Path

__version__ = "0.1.0"

# The context engine (`sf_context_engine`) keeps its state under SF_CONTEXT_HOME.
# The agent historically stored it in <repo>/.cache; keep that as the agent's
# default so existing indexes / warmup sentinels are still found.
os.environ.setdefault(
    "SF_CONTEXT_HOME", str(Path(__file__).resolve().parent.parent.parent / ".cache")
)
