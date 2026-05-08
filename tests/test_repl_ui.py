"""Unit tests for the REPL UI rendering helpers (Phase C v1).

Renders into a `Console(file=StringIO, force_terminal=False)` so the captured
plain text contains the actual glyphs we care about. ANSI sequences are off
in non-terminal mode so assertions can match on substrings.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from sf_dev_agent import repl_ui


@pytest.fixture
def capture():
    """Swap in a non-terminal Console so output is captured cleanly."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=200)
    repl_ui.set_console_for_tests(console)
    yield buf
    repl_ui.set_console_for_tests(Console())


# ---------------------------------------------------------------------------
# Input summarization
# ---------------------------------------------------------------------------

def test_format_tool_input_summary_uses_key_value_form() -> None:
    summary = repl_ui.format_tool_input_summary({"query": "Account", "limit": 5})
    assert summary == 'query="Account", limit=5'


def test_format_tool_input_summary_collapses_long_strings() -> None:
    blob = "x" * 500
    summary = repl_ui.format_tool_input_summary({"source": blob})
    assert "<str 500 chars>" in summary
    assert blob not in summary


def test_format_tool_input_summary_collapses_lists_and_dicts() -> None:
    summary = repl_ui.format_tool_input_summary(
        {"ids": [1, 2, 3], "params": {"a": 1, "b": 2}}
    )
    assert "ids=<list 3>" in summary
    assert "params=<dict 2 keys>" in summary


def test_format_tool_input_summary_truncates_to_max_len() -> None:
    summary = repl_ui.format_tool_input_summary(
        {f"k{i}": f"v{i}" for i in range(40)}, max_len=80
    )
    assert len(summary) <= 80
    assert summary.endswith("…")


def test_format_tool_input_summary_falls_back_to_json_for_non_dict() -> None:
    assert repl_ui.format_tool_input_summary("hello") == '"hello"'
    assert repl_ui.format_tool_input_summary([1, 2, 3]) == "[1, 2, 3]"


def test_format_tool_input_summary_handles_empty_dict() -> None:
    assert repl_ui.format_tool_input_summary({}) == "{}"


# ---------------------------------------------------------------------------
# Lifecycle renderers
# ---------------------------------------------------------------------------

def test_render_tool_call_header_emits_name_and_summary(capture) -> None:
    repl_ui.render_tool_call_header("code_search", {"query": "Account"})
    out = capture.getvalue()
    assert "code_search" in out
    assert "query=" in out


def test_render_tool_ok_emits_char_count(capture) -> None:
    repl_ui.render_tool_ok("semantic_search", "x" * 1234)
    out = capture.getvalue()
    assert "semantic_search" in out
    assert "1234" in out
    assert "✓" in out


def test_render_tool_error_truncates_long_error(capture) -> None:
    long_err = "BoomError: " + "x" * 500
    repl_ui.render_tool_error("build_metadata_index", long_err)
    out = capture.getvalue()
    assert "build_metadata_index" in out
    assert "BoomError" in out
    assert "✗" in out
    # Long error truncated by the renderer.
    assert "x" * 500 not in out


def test_render_tool_blocked_includes_reason(capture) -> None:
    repl_ui.render_tool_blocked(
        "deploy_metadata", "requires an approved plan before execution"
    )
    out = capture.getvalue()
    assert "deploy_metadata" in out
    assert "approved plan" in out
    assert "⊘" in out


def test_tool_status_no_op_in_non_terminal(capture) -> None:
    """The non-terminal capture console is_terminal=False — spinner should
    no-op without raising and without leaking spinner glyphs into the buffer."""
    with repl_ui.tool_status("anything"):
        pass
    # No new content from the spinner itself.
    assert "running anything" not in capture.getvalue()


def test_render_streaming_text_writes_delta(capture) -> None:
    repl_ui.render_streaming_text("hello ")
    repl_ui.render_streaming_text("world")
    repl_ui.render_stream_terminator()
    out = capture.getvalue()
    assert "hello " in out
    assert "world" in out
    # Terminator pushes a newline so the next prompt starts fresh.
    assert out.endswith("\n")


# ---------------------------------------------------------------------------
# v2 slice 1 — render_file_write_diff
# ---------------------------------------------------------------------------

def test_render_file_write_diff_new_file_shows_additions(capture) -> None:
    repl_ui.render_file_write_diff(
        "src/foo.cls", before="",
        after="line one\nline two\nline three\n",
    )
    out = capture.getvalue()
    # The unified-diff header lines name the file with (before)/(after) tags.
    assert "src/foo.cls (before)" in out
    assert "src/foo.cls (after)" in out
    # Hunk header is present.
    assert "@@" in out
    # All content lines appear with the `+` prefix from unified diff.
    assert "+line one" in out
    assert "+line two" in out
    assert "+line three" in out
    # No deletion lines for a new file.
    assert "-line" not in out


def test_render_file_write_diff_edit_shows_both_sides(capture) -> None:
    before = "alpha\nbeta\ngamma\n"
    after = "alpha\nBETA\ngamma\n"
    repl_ui.render_file_write_diff("src/foo.cls", before, after)
    out = capture.getvalue()
    # Edit produces both a `-` and a `+` line for the changed row.
    assert "-beta" in out
    assert "+BETA" in out
    # Context lines from unified diff are present unprefixed.
    assert "alpha" in out
    assert "gamma" in out
    # Vertical-bar prefix from the renderer for visual nesting.
    assert "│" in out


def test_render_file_write_diff_no_op_skips_render(capture) -> None:
    """Identical before/after produces no output — caller relies on the OK
    line to acknowledge the (no-op) write."""
    repl_ui.render_file_write_diff("src/foo.cls", "same\n", "same\n")
    assert capture.getvalue() == ""


def test_render_file_write_diff_truncates_long_diff(capture) -> None:
    """Caps output at max_lines so one giant write doesn't flood the screen."""
    before = ""
    after = "\n".join(f"line{i}" for i in range(500)) + "\n"
    repl_ui.render_file_write_diff(
        "src/big.cls", before=before, after=after, max_lines=20,
    )
    out = capture.getvalue()
    assert "diff truncated at 20 lines" in out
    # Lines beyond the cap should not appear (line499 is well past the cap).
    assert "+line499" not in out


def test_render_file_write_diff_handles_rich_markup_chars(capture) -> None:
    """Arbitrary file content with rich-markup-shaped chars (`[red]`, `[/]`)
    must render literally, not as styling — we use Text, not markup."""
    before = ""
    after = "x = [red]not a tag[/red]\n"
    repl_ui.render_file_write_diff("src/foo.cls", before, after)
    out = capture.getvalue()
    # The literal bracketed text appears in the output.
    assert "[red]not a tag[/red]" in out
