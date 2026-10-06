"""Data-directory resolution for the context engine.

All persistent state (SQLite index, retrieve scratch dirs, memory exports)
lives under one directory so the package works the same when installed
into site-packages as when run from a checkout.

Resolution: ``SF_CONTEXT_HOME`` env var, else ``~/.sf-context``.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def data_dir() -> Path:
    env = os.environ.get("SF_CONTEXT_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".sf-context"
