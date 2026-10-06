"""Regression tests for running `sf` from inside an MCP server."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from sf_context_engine import delta, retriever
from sf_context_engine.sfcli import sf_env, strip_ansi

COLOURED = '\x1b[97m{\x1b[39m\n  \x1b[94m"status"\x1b[39m: \x1b[34m0\x1b[39m\n\x1b[97m}\x1b[39m'


def test_strip_ansi_makes_coloured_sf_json_parseable():
    assert json.loads(strip_ansi(COLOURED)) == {"status": 0}


def test_sf_env_disables_colour(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "3")
    env = sf_env()
    assert env["FORCE_COLOR"] == "0" and env["NO_COLOR"] == "1"


def test_sf_subprocesses_do_not_inherit_stdin_and_tolerate_colour(monkeypatch, tmp_path):
    """An MCP server's stdin is the protocol pipe; a child `sf` must not read it."""
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=COLOURED, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    retriever.retrieve("Org", ["ApexClass"], tmp_path)
    records, err = delta._query_tooling("Org", "SELECT Id FROM ApexClass", 30)
    assert len(seen) == 2, "expected one retrieve call and one tooling query"
    assert err is None  # coloured JSON was parsed, not rejected as non-JSON
    for kwargs in seen:
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["FORCE_COLOR"] == "0"
