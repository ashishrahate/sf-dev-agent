"""Unit tests for `_capture_file_write_before` (REPL UI v2 slice 1).

Covers the security-sensitive path-traversal check + the new-file = ""
convention that drives the inline diff rendering. The function is a
free helper in `agent.py` (not a method) so we can test it in isolation
without standing up a full AgentLoop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_dev_agent.agent import _capture_file_write_before


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AGENT_WORKSPACE at a tmp dir for the duration of the test."""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    return tmp_path


def test_capture_returns_existing_content(workspace: Path) -> None:
    """A file already in the workspace returns its content for diffing."""
    target = workspace / "existing.cls"
    target.write_text("public class A { void m() {} }\n", encoding="utf-8")

    result = _capture_file_write_before({"file_path": "existing.cls"})

    assert result == "public class A { void m() {} }\n"


def test_capture_returns_empty_string_for_new_file(workspace: Path) -> None:
    """File doesn't exist yet → "" so the renderer shows all additions."""
    result = _capture_file_write_before({"file_path": "brand-new.cls"})

    assert result == ""


def test_capture_returns_none_on_path_traversal(workspace: Path) -> None:
    """`../etc/passwd` style paths must NOT escape the workspace.

    The capture short-circuits to None so the diff is skipped — the
    executor's own validation rejects the actual write.
    """
    result = _capture_file_write_before({"file_path": "../escape.txt"})

    assert result is None


def test_capture_returns_none_for_missing_file_path(workspace: Path) -> None:
    """Defensive: tool_input lacking file_path → None, not crash."""
    assert _capture_file_write_before({}) is None
    assert _capture_file_write_before({"file_path": ""}) is None
    assert _capture_file_write_before({"file_path": None}) is None


def test_capture_handles_subdirectories(workspace: Path) -> None:
    """Nested paths within workspace resolve correctly."""
    nested = workspace / "subdir" / "nested.cls"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested content\n", encoding="utf-8")

    result = _capture_file_write_before({"file_path": "subdir/nested.cls"})

    assert result == "nested content\n"
