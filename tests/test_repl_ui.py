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


# ---------------------------------------------------------------------------
# v2 slice 3 — collapsible tool blocks
# ---------------------------------------------------------------------------

@pytest.fixture
def buffer_registered():
    """Register a fresh dict as the session tool-output buffer for the test,
    then detach afterward so other tests stay isolated."""
    buf: dict = {}
    repl_ui.set_tool_output_buffer(buf)
    yield buf
    repl_ui.set_tool_output_buffer(None)


def test_render_tool_ok_no_buffer_keeps_one_line(capture) -> None:
    """Backwards compat: when no buffer is registered, render_tool_ok prints
    only the success line — no preview, no hint, no buffering happens."""
    repl_ui.render_tool_ok("code_search", "x\n" * 100)
    out = capture.getvalue()
    assert "code_search" in out
    assert "✓" in out
    # No preview body, no hint.
    assert "more line" not in out
    assert "Ctrl+O" not in out


def test_render_tool_ok_short_result_no_preview(capture, buffer_registered) -> None:
    """Short result: buffer captures the payload but no preview is shown."""
    payload = "ok\nshort\n"
    repl_ui.render_tool_ok("ping", payload, tool_use_id="toolu_short01")
    out = capture.getvalue()
    assert "ping" in out
    assert "more line" not in out
    assert "Ctrl+O" not in out
    # Captured for later Ctrl+O recall.
    assert buffer_registered["toolu_short01"]["payload"] == payload
    assert buffer_registered["toolu_short01"]["tool_name"] == "ping"
    assert buffer_registered["toolu_short01"]["is_error"] is False


def test_render_tool_ok_long_result_renders_preview_and_hint(
    capture, buffer_registered,
) -> None:
    """Long result: preview shows the first N lines + a Ctrl+O hint."""
    payload = "\n".join(f"line{i}" for i in range(40))
    repl_ui.render_tool_ok("retrieve_context", payload, tool_use_id="toolu_long01")
    out = capture.getvalue()
    threshold = repl_ui.get_collapse_lines_threshold()
    # First N lines appear in the preview body.
    for i in range(threshold):
        assert f"line{i}" in out
    # `… <n> more lines — press Ctrl+O to expand` hint.
    remaining = 40 - threshold
    assert f"{remaining} more line" in out
    assert "Ctrl+O" in out
    # The id isn't user-facing anymore (only the last buffered output is
    # reachable via Ctrl+O) — it just lives in the buffer for the keybind.
    assert buffer_registered["toolu_long01"]["payload"] == payload


def test_render_tool_ok_submit_plan_skips_preview(
    capture, buffer_registered,
) -> None:
    """submit_plan has its own plan rendering — don't double up with the
    collapse preview. Still captured for Ctrl+O though."""
    payload = "\n".join(f"step{i}" for i in range(20))
    repl_ui.render_tool_ok("submit_plan", payload, tool_use_id="toolu_plan01")
    out = capture.getvalue()
    assert "submit_plan" in out
    assert "more line" not in out
    assert "Ctrl+O" not in out
    assert "toolu_plan01" in buffer_registered


def test_render_tool_error_captures_but_skips_preview(
    capture, buffer_registered,
) -> None:
    """Errors get buffered for Ctrl+O but never preview-rendered — the
    truncated one-liner is already visible and a multi-line stack trace
    would defeat the truncation."""
    err = "BoomError: " + "\n".join(f"frame{i}" for i in range(30))
    repl_ui.render_tool_error("code_search", err, tool_use_id="toolu_err01")
    out = capture.getvalue()
    # Truncated error line rendered as before.
    assert "BoomError" in out
    assert "✗" in out
    # No preview body, no hint.
    assert "more line" not in out
    assert "Ctrl+O" not in out
    # Captured with is_error=True for the Ctrl+O renderer to color red.
    assert buffer_registered["toolu_err01"]["is_error"] is True
    assert buffer_registered["toolu_err01"]["payload"] == err


def test_set_collapse_lines_threshold_clamps_negative() -> None:
    """Negative thresholds clamp to 0 so we never index lines[:-N]."""
    prev = repl_ui.get_collapse_lines_threshold()
    try:
        repl_ui.set_collapse_lines_threshold(-5)
        assert repl_ui.get_collapse_lines_threshold() == 0
    finally:
        repl_ui.set_collapse_lines_threshold(prev)


def test_threshold_zero_previews_everything(capture, buffer_registered) -> None:
    """Threshold=0 means every multi-line output shows the hint (no head
    preview), still captures. Useful when the user wants pure Ctrl+O UX."""
    prev = repl_ui.get_collapse_lines_threshold()
    try:
        repl_ui.set_collapse_lines_threshold(0)
        payload = "a\nb\nc"
        repl_ui.render_tool_ok("t", payload, tool_use_id="toolu_zero")
        out = capture.getvalue()
        assert "3 more line" in out
        assert "Ctrl+O" in out
    finally:
        repl_ui.set_collapse_lines_threshold(prev)


def test_render_expanded_tool_output_success(capture) -> None:
    """The expand renderer frames the payload with a cyan border + 'output' label."""
    entry = {
        "tool_name": "code_search",
        "payload": "line1\nline2\nline3",
        "is_error": False,
    }
    repl_ui.render_expanded_tool_output(entry)
    out = capture.getvalue()
    assert "code_search" in out
    assert "output" in out
    assert "line1" in out
    assert "line3" in out
    assert "end expand" in out


def test_render_expanded_tool_output_error(capture) -> None:
    """Errors get a red border + 'error' label."""
    entry = {
        "tool_name": "sf_source_deploy",
        "payload": "stack trace body",
        "is_error": True,
    }
    repl_ui.render_expanded_tool_output(entry)
    out = capture.getvalue()
    assert "sf_source_deploy" in out
    assert "error" in out
    assert "stack trace body" in out


def test_buffer_detach_after_session() -> None:
    """`set_tool_output_buffer(None)` cleanly detaches — subsequent renders
    don't crash and don't try to write to a stale dict."""
    repl_ui.set_tool_output_buffer({})
    repl_ui.set_tool_output_buffer(None)
    # Should not raise. No fixture/capture needed — we just want a clean
    # signal that the no-op path doesn't NPE.
    repl_ui.render_tool_ok("ping", "ok", tool_use_id="toolu_x")
