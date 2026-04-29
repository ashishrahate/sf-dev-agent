"""Tool-call rendering helpers for the REPL — Claude-Code-style v1.

Centralizes how tool calls and their results show up in the terminal so the
agent loop doesn't sprinkle ad-hoc `console.print` calls. Goals:

- A consistent visual language: every tool call has the same header/result
  shape, regardless of which provider, which tool, or which phase.
- A spinner during tool execution so long calls (build_metadata_index,
  semantic_search the first time) don't look frozen.
- Compact input summaries that read better than `json.dumps(...)[:200]`
  and don't blow up on multiline values (a pasted Apex blob, for instance).

v2 — deferred until v1 lands and we have signal on what's missing:
- Inline diffs for file-mutating tools (need a tool-classification table).
- Collapsible tool blocks via rich `Group`/`Tree` rendering.
- Syntax highlighting for code-shaped tool results.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Iterator

from rich.console import Console

# A single shared console keeps spinner state coherent. Tests can substitute
# a Console pointed at a StringIO via `set_console_for_tests`.
_console: Console = Console()


def set_console_for_tests(console: Console) -> None:
    """Swap the module-level console — only intended for unit tests."""
    global _console
    _console = console


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


def render_tool_ok(tool_name: str, result_str: str) -> None:
    """Success line. Char count gives a sense of result volume without
    dumping the full payload (the LLM still gets the full text)."""
    _console.print(
        f"[green]└ ✓[/green] [dim]{tool_name} → {len(result_str)} chars[/dim]"
    )


def render_tool_error(tool_name: str, error_msg: str) -> None:
    """Error line. Truncated to keep one tool's failure from eating the screen."""
    _console.print(
        f"[red]└ ✗[/red] [bold red]{tool_name}[/bold red] "
        f"[red]{_truncate(error_msg, 200)}[/red]"
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
    "get_console",
    "render_streaming_text",
    "render_stream_terminator",
    "render_tool_blocked",
    "render_tool_call_header",
    "render_tool_error",
    "render_tool_ok",
    "set_console_for_tests",
    "tool_status",
]
