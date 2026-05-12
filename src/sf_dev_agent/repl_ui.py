"""Tool-call rendering helpers for the REPL — Claude-Code-style v1+v2.

Centralizes how tool calls and their results show up in the terminal so the
agent loop doesn't sprinkle ad-hoc `console.print` calls. Goals:

- A consistent visual language: every tool call has the same header/result
  shape, regardless of which provider, which tool, or which phase.
- A spinner during tool execution so long calls (build_metadata_index,
  semantic_search the first time) don't look frozen.
- Compact input summaries that read better than `json.dumps(...)[:200]`
  and don't blow up on multiline values (a pasted Apex blob, for instance).

v2 — landing in slices:
- Slice 1 ✅ Inline diffs for `file_write` (this module's `render_file_write_diff`).
- Slice 2 (deferred): syntax highlighting for code-shaped tool results.
- Slice 3 ✅ Collapsible / expandable tool blocks via tool_use_id capture
  (head-N-lines preview + /expand <id> recovers the full payload).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
from typing import Any, Iterator

from rich.console import Console
from rich.text import Text

# A single shared console keeps spinner state coherent. Tests can substitute
# a Console pointed at a StringIO via `set_console_for_tests`.
_console: Console = Console()

# Collapse / expand state — v2 slice 3.
#
# `_TOOL_OUTPUT_BUFFER` is set by the REPL session at launch and cleared on
# exit. While set, `render_tool_ok` / `render_tool_error` insert each tool
# result so `/expand <id>` can recover the full payload later. Insertion
# order is preserved so `/expand last` resolves to the most recent call.
# When the buffer is None (one-shot CLI runs, tests that don't opt in), the
# render path is unchanged.
_TOOL_OUTPUT_BUFFER: dict[str, dict[str, Any]] | None = None

# How many head lines of a long tool result to preview inline before the
# `… N more lines` hint. Configurable via `REPL_COLLAPSE_LINES` so users
# can dial it up/down without code changes.
_DEFAULT_COLLAPSE_LINES = 5
try:
    _COLLAPSE_LINES_THRESHOLD: int = max(
        0, int(os.environ.get("REPL_COLLAPSE_LINES", _DEFAULT_COLLAPSE_LINES))
    )
except ValueError:
    _COLLAPSE_LINES_THRESHOLD = _DEFAULT_COLLAPSE_LINES

# Tools whose results never get the preview-and-hint treatment. submit_plan
# renders its plan elsewhere; we don't want to double-render or confuse the
# user with a `/expand` hint they'd never use.
_NO_PREVIEW_TOOLS: frozenset[str] = frozenset({"submit_plan"})


def set_console_for_tests(console: Console) -> None:
    """Swap the module-level console — only intended for unit tests."""
    global _console
    _console = console


def set_tool_output_buffer(buf: dict[str, dict[str, Any]] | None) -> None:
    """Register a session-scoped buffer for `/expand` recall.

    Pass a dict (preserves insertion order in Python 3.7+) to opt in; pass
    `None` to detach. Caller owns the buffer's lifetime — the REPL clears
    its own copy at session end so memory doesn't grow across sessions.
    """
    global _TOOL_OUTPUT_BUFFER
    _TOOL_OUTPUT_BUFFER = buf


def set_collapse_lines_threshold(n: int) -> None:
    """Override the inline-preview threshold. Mostly for tests; production
    callers use the `REPL_COLLAPSE_LINES` env var instead."""
    global _COLLAPSE_LINES_THRESHOLD
    _COLLAPSE_LINES_THRESHOLD = max(0, n)


def get_collapse_lines_threshold() -> int:
    """Accessor — useful for tests that need to assert against the active value."""
    return _COLLAPSE_LINES_THRESHOLD


def get_console() -> Console:
    """Accessor for the active console (used by callers that need rich state)."""
    return _console


# ---------------------------------------------------------------------------
# Input summarization
# ---------------------------------------------------------------------------

def format_tool_input_summary(tool_input: Any, max_len: int = 120) -> str:
    """Compact one-line view of a tool's input dict.

    Prefers `key=value` form (more readable than raw JSON for short dicts).
    Falls back to truncated JSON when values are nested or very long.
    Multi-line strings collapse to `<N chars>` so a pasted blob doesn't
    smear across the terminal.
    """
    if not isinstance(tool_input, dict) or not tool_input:
        s = json.dumps(tool_input, ensure_ascii=False)
        return _truncate(s, max_len)

    parts: list[str] = []
    for key, value in tool_input.items():
        parts.append(f"{key}={_summarize_value(value)}")
    summary = ", ".join(parts)
    return _truncate(summary, max_len)


def _summarize_value(value: Any) -> str:
    if isinstance(value, str):
        if "\n" in value or len(value) > 60:
            return f"<str {len(value)} chars>"
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return f"<list {len(value)}>"
    if isinstance(value, dict):
        return f"<dict {len(value)} keys>"
    return json.dumps(value, ensure_ascii=False)


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# Rendering primitives — one per state in a tool call's lifecycle
# ---------------------------------------------------------------------------

def render_tool_call_header(tool_name: str, tool_input: Any) -> None:
    """First line printed when a tool call begins.

    Format: `┌ <tool_name>  <input summary>`
    """
    summary = format_tool_input_summary(tool_input)
    _console.print(
        f"[cyan]┌[/cyan] [bold cyan]{tool_name}[/bold cyan]  "
        f"[dim]{summary}[/dim]"
    )


def render_tool_ok(
    tool_name: str,
    result_str: str,
    tool_use_id: str | None = None,
) -> None:
    """Success line. Char count gives a sense of result volume without
    dumping the full payload (the LLM still gets the full text).

    When a REPL session has registered a tool-output buffer AND a
    `tool_use_id` is supplied, the full result is stored for later
    `/expand <id>` recall. For long results, a head-N-lines preview is
    printed under the success line with a hint pointing at `/expand`.
    """
    _console.print(
        f"[green]└ ✓[/green] [dim]{tool_name} → {len(result_str)} chars[/dim]"
    )
    _maybe_buffer_and_preview(tool_name, result_str, tool_use_id, is_error=False)


def render_tool_error(
    tool_name: str,
    error_msg: str,
    tool_use_id: str | None = None,
) -> None:
    """Error line. Truncated to keep one tool's failure from eating the screen.

    Like the success path, the full error is captured into the session
    buffer (if one is registered) so `/expand <id>` can recover the
    untrimmed text — useful for debugging failures where the truncated
    surface drops the stack trace.
    """
    _console.print(
        f"[red]└ ✗[/red] [bold red]{tool_name}[/bold red] "
        f"[red]{_truncate(error_msg, 200)}[/red]"
    )
    # Errors get captured but never preview-rendered — the truncated
    # one-liner above is already visible; `/expand` reveals the rest.
    _maybe_buffer_and_preview(tool_name, error_msg, tool_use_id, is_error=True)


def _maybe_buffer_and_preview(
    tool_name: str,
    payload: str,
    tool_use_id: str | None,
    *,
    is_error: bool,
) -> None:
    """Store `payload` in the session buffer (if registered) and render a
    head-N-lines preview for long success outputs.

    Errors are stored but not previewed — the truncated render_tool_error
    line is already visible to the user, and a multi-line stack trace
    would defeat the point of the truncation. `/expand <id>` still
    recovers the full text.
    """
    if _TOOL_OUTPUT_BUFFER is None or not tool_use_id:
        return

    _TOOL_OUTPUT_BUFFER[tool_use_id] = {
        "tool_name": tool_name,
        "payload": payload,
        "is_error": is_error,
    }

    if is_error or tool_name in _NO_PREVIEW_TOOLS:
        return

    lines = payload.splitlines()
    if len(lines) <= _COLLAPSE_LINES_THRESHOLD:
        return

    # Show first N lines under the success line, then a hint with the
    # tool_use_id (or its 12-char prefix when very long) so the user can
    # `/expand` to see the rest.
    short_id = tool_use_id if len(tool_use_id) <= 16 else tool_use_id[:12]
    for line in lines[:_COLLAPSE_LINES_THRESHOLD]:
        _console.print(Text("│ ", style="cyan") + Text(line, style="dim"))
    remaining = len(lines) - _COLLAPSE_LINES_THRESHOLD
    _console.print(
        Text("│ ", style="cyan")
        + Text(
            f"… {remaining} more line{'s' if remaining != 1 else ''} "
            f"— /expand {short_id}",
            style="dim italic",
        )
    )


def render_tool_blocked(tool_name: str, reason: str) -> None:
    """Gating block (e.g., write tool during planning phase)."""
    _console.print(
        f"[yellow]└ ⊘[/yellow] [bold yellow]{tool_name} blocked[/bold yellow] "
        f"[dim]{_truncate(reason, 200)}[/dim]"
    )


@contextlib.contextmanager
def tool_status(tool_name: str) -> Iterator[None]:
    """Context manager that shows a spinner while a tool runs.

    Falls back to a no-op when the console is non-interactive (e.g., output
    is being captured for tests). The header has already been printed by the
    caller, so on no-op we just yield silently — the result line comes after.
    """
    if not _console.is_terminal:
        yield
        return
    with _console.status(
        f"[dim]running {tool_name}…[/dim]",
        spinner="dots",
        spinner_style="cyan",
    ):
        yield


# ---------------------------------------------------------------------------
# Inline diff for file_write (v2 slice 1)
# ---------------------------------------------------------------------------

def render_file_write_diff(
    file_path: str, before: str, after: str, max_lines: int = 200,
) -> None:
    """Print a colored unified diff between header and footer of a tool block.

    Skipped silently when content is unchanged so a no-op write doesn't
    print an empty diff. Each diff line is prefixed with `│ ` for visual
    nesting under the v1 `┌`/`└` block. Built with `rich.text.Text` so
    arbitrary file content can't smuggle in rich markup.

    Truncates to `max_lines` to keep one giant write from filling the
    screen — the LLM still sees the full result via the executor's return.
    """
    if before == after:
        return

    diff_lines = list(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"{file_path} (before)",
        tofile=f"{file_path} (after)",
        lineterm="",
    ))
    if not diff_lines:
        return

    truncated = False
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines]
        truncated = True

    for line in diff_lines:
        prefix = Text("│ ", style="cyan")
        if line.startswith("---") or line.startswith("+++"):
            body = Text(line, style="dim")
        elif line.startswith("@@"):
            body = Text(line, style="magenta")
        elif line.startswith("+") and not line.startswith("+++"):
            body = Text(line, style="green")
        elif line.startswith("-") and not line.startswith("---"):
            body = Text(line, style="red")
        else:
            body = Text(line, style="dim")
        _console.print(prefix + body)

    if truncated:
        _console.print(
            Text("│ ", style="cyan")
            + Text(f"… diff truncated at {max_lines} lines", style="dim italic")
        )


# ---------------------------------------------------------------------------
# Auto-reindex summary (post-write hook)
# ---------------------------------------------------------------------------

def render_reindex_summary(
    components: int = 0,
    relationships: int = 0,
    embedded: int = 0,
    skipped: int = 0,
) -> None:
    """One-line note printed between the diff render and the OK line.

    Format: ``│ ↻ indexed 1 component, 3 relationships, 1 embedded``

    Parts whose count is zero are omitted to keep the happy path quiet
    (e.g., a no-relationship file just prints "indexed 1 component").
    Skipped count is suffixed in parens only when > 0.

    Caller skips the call entirely if there's nothing meaningful to
    show (no components AND no skipped); this function trusts the
    caller and unconditionally prints what it's given.
    """
    parts: list[str] = []
    if components:
        parts.append(
            f"{components} component{'s' if components != 1 else ''}"
        )
    if relationships:
        parts.append(
            f"{relationships} relationship{'s' if relationships != 1 else ''}"
        )
    if embedded:
        parts.append(
            f"{embedded} embedded"
        )
    if not parts:
        return
    msg = "indexed " + ", ".join(parts)
    if skipped:
        msg += f" ({skipped} skipped)"
    _console.print(
        Text("│ ", style="cyan") + Text(f"↻ {msg}", style="dim cyan")
    )


# ---------------------------------------------------------------------------
# Streaming-text helpers
# ---------------------------------------------------------------------------

def render_streaming_text(text: str) -> None:
    """Print one streaming delta. Soft-wrap so we don't break inside tokens."""
    _console.print(text, end="", soft_wrap=True)


def render_stream_terminator() -> None:
    """Newline after the model finishes its streamed text. Idempotent enough
    that callers can invoke it whenever they're unsure if the cursor is on
    a fresh line."""
    _console.print()


__all__ = [
    "format_tool_input_summary",
    "get_collapse_lines_threshold",
    "get_console",
    "render_file_write_diff",
    "render_reindex_summary",
    "render_streaming_text",
    "render_stream_terminator",
    "render_tool_blocked",
    "render_tool_call_header",
    "render_tool_error",
    "render_tool_ok",
    "set_collapse_lines_threshold",
    "set_console_for_tests",
    "set_tool_output_buffer",
    "tool_status",
]
