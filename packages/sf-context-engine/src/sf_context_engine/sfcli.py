"""Helpers for running the `sf` CLI from a library or MCP server.

When this package runs inside another tool (an MCP client, CI, an IDE terminal)
the environment may force ANSI colour, which `sf --json` then embeds in its JSON
output and breaks parsing. Disable colour for our subprocesses and strip any
escape codes that still appear.
"""

from __future__ import annotations

import os
import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sf_env() -> dict[str, str]:
    return {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0", "SF_NO_COLOR": "true"}


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)
