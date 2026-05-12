"""Slash-command registry for the persistent REPL (Phase C.1).

One function per slash command. Each handler takes a `ReplSession` plus
the parsed argv (everything after the command word), renders to console,
and returns a `ReplDirective` telling the loop what to do next:

    CONTINUE  — keep looping, take the next prompt.
    QUIT      — leave the REPL.

Slash commands are intentionally thin shells over existing modules
(`memory_cli.run_memory_command`, `resume_cli.run_resume_command`,
`warmup.run_warmup`, etc.) so they're well-tested before C.1 even runs.
The REPL adds the ergonomics; the substance is shared with the one-shot
CLI verbs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from sf_dev_agent.repl import ReplSession

console = Console()
logger = logging.getLogger(__name__)


class ReplDirective(StrEnum):
    CONTINUE = "continue"
    QUIT = "quit"


# Each handler signature: (session, argv) -> ReplDirective.
SlashHandler = Callable[["ReplSession", list[str]], ReplDirective]


@dataclass(frozen=True)
class SlashCommand:
    name: str                 # canonical name with leading slash, e.g. "/help"
    summary: str              # one-liner shown in /help
    handler: SlashHandler     # invoked with (session, argv)


# Filled at module bottom after all handlers are defined.
SLASH_COMMANDS: dict[str, SlashCommand] = {}


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------

def cmd_help(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Render a table of every slash command + free-form usage."""
    table = Table(
        title="sf-agent REPL — slash commands",
        header_style="bold cyan",
    )
    table.add_column("Command", style="bold")
    table.add_column("What it does", overflow="fold")
    for name in sorted(SLASH_COMMANDS):
        table.add_row(name, SLASH_COMMANDS[name].summary)
    console.print(table)
    console.print(
        "\n[dim]Free-form input (no leading /) is sent to the agent as a "
        "task. The plan -> approve -> execute flow runs the same as one-shot "
        "[cyan]sf-agent \"...\"[/cyan].[/dim]"
    )
    return ReplDirective.CONTINUE


def cmd_quit(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Leave the REPL. Triggers the extract nudge in C.5."""
    return ReplDirective.QUIT


def cmd_clear(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Clear the screen. Persistent state (memory, working memory) is unaffected."""
    console.clear()
    console.print("[dim]Screen cleared. Memory and task state preserved.[/dim]")
    return ReplDirective.CONTINUE


def cmd_status(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Print the same data the bottom-toolbar shows, but with rationale."""
    from sf_dev_agent.repl import format_status_dict
    status = format_status_dict(session)

    table = Table(title="Session status", header_style="bold cyan", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for key, value in status.items():
        table.add_row(key, value)
    console.print(table)
    return ReplDirective.CONTINUE


def cmd_index(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Run the same warm-up sequence as the first-run prompt: build_index
    (delta) + embed components + embed knowledge."""
    from sf_dev_agent.warmup import run_warmup

    if session.mock_org:
        console.print(
            "[yellow]/index is a no-op in mock-org mode.[/yellow] "
            "Toggle off with [cyan]/mock off[/cyan] first."
        )
        return ReplDirective.CONTINUE

    run_warmup(org=session.org)
    return ReplDirective.CONTINUE


def cmd_resume(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Delegate to the resume CLI; supports `/resume <id>`, `/resume --list`,
    `/resume --latest`. Same flag set as `sf-agent resume`."""
    from sf_dev_agent.resume_cli import run_resume_command

    # Pass through to the existing CLI argv parser. /resume with no args
    # falls back to --list (helpful default in REPL context).
    if not argv:
        argv = ["--list"]
    run_resume_command(argv)
    return ReplDirective.CONTINUE


def cmd_tasks(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Show recent tasks in scope. Alias for `/resume --list` plus terminal."""
    from sf_dev_agent.memory import (
        TERMINAL_STATUSES,
        MemoryScope,
        WorkingMemoryStore,
    )

    include_terminal = "--all" in argv or "--include-terminal" in argv

    from sf_dev_agent.context import default_db_path
    db_path = default_db_path()

    scope = MemoryScope(
        tenant_id=session.org.tenant_id, org_alias=session.org.org_alias,
    )
    with WorkingMemoryStore(db_path) as store:
        rows = store.list_tasks(scope=scope, limit=20)

    if not rows:
        console.print("[dim]No tasks in this scope yet.[/dim]")
        return ReplDirective.CONTINUE

    if not include_terminal:
        rows = [r for r in rows if r.status not in TERMINAL_STATUSES]
        if not rows:
            console.print(
                "[dim]No in-flight tasks. Pass --all to see completed/failed runs.[/dim]"
            )
            return ReplDirective.CONTINUE

    table = Table(title="Recent tasks", header_style="bold cyan")
    table.add_column("Task ID", style="bold")
    table.add_column("Status")
    table.add_column("Description", overflow="fold")
    for r in rows:
        desc = r.user_request.strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        table.add_row(r.id, r.status, desc)
    console.print(table)
    return ReplDirective.CONTINUE


def cmd_memory(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Delegate to the memory CLI. /memory recall|list|extract|export|promote."""
    from sf_dev_agent.memory_cli import run_memory_command
    if not argv:
        console.print(
            "[yellow]Usage:[/yellow] /memory <recall|list|extract|export|promote> [...]"
        )
        return ReplDirective.CONTINUE
    run_memory_command(argv)
    return ReplDirective.CONTINUE


def cmd_mock(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Toggle mock-org mode mid-session. /mock on|off|toggle (default toggle)."""
    if not argv or argv[0] in ("toggle", ""):
        session.mock_org = not session.mock_org
    elif argv[0] in ("on", "true", "1"):
        session.mock_org = True
    elif argv[0] in ("off", "false", "0"):
        session.mock_org = False
    else:
        console.print("[yellow]Usage:[/yellow] /mock on|off|toggle")
        return ReplDirective.CONTINUE
    state = "[bold yellow]ON[/bold yellow]" if session.mock_org else "[green]OFF[/green]"
    console.print(f"mock-org mode: {state}")
    return ReplDirective.CONTINUE


def cmd_provider(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Switch LLM provider mid-session. /provider gemini|openai|anthropic [model]."""
    from sf_dev_agent.providers import PROVIDERS, create_provider

    if not argv:
        console.print(
            f"[yellow]Usage:[/yellow] /provider <{'|'.join(PROVIDERS)}> [model]"
        )
        return ReplDirective.CONTINUE

    name = argv[0]
    if name not in PROVIDERS:
        console.print(f"[red]Unknown provider:[/red] {name}")
        return ReplDirective.CONTINUE

    model = argv[1] if len(argv) > 1 else None

    try:
        new_provider = create_provider(provider=name, model=model)
    except (ImportError, ValueError) as exc:
        console.print(f"[red]Provider switch failed:[/red] {exc}")
        return ReplDirective.CONTINUE

    session.provider = new_provider
    console.print(
        f"Switched to [bold]{new_provider.__class__.__name__}[/bold] "
        f"(model: [cyan]{new_provider.model_name}[/cyan])"
    )
    return ReplDirective.CONTINUE


def cmd_mode(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Show or set the operating mode. /mode | /mode plan|execution|general.

    Equivalent to the Shift+Tab keybinding for users who prefer typing.
    No-op + usage line on invalid arg. The new mode applies to NEXT
    task — current/resumed tasks honor their own persisted mode.
    """
    from sf_dev_agent.models.schemas import AgentMode
    from sf_dev_agent.repl import format_mode_label

    if not argv:
        console.print(
            f"current mode: {format_mode_label(session.mode)} "
            f"[dim](Shift+Tab to cycle, or /mode <plan|execution|general>)[/dim]"
        )
        return ReplDirective.CONTINUE

    arg = argv[0].lower()
    valid = {m.value for m in AgentMode}
    if arg not in valid:
        console.print(
            f"[red]Unknown mode:[/red] {arg}. "
            f"[yellow]Usage:[/yellow] /mode <{ '|'.join(sorted(valid)) }>"
        )
        return ReplDirective.CONTINUE

    session.mode = AgentMode(arg)
    console.print(f"mode set to {format_mode_label(session.mode)}")
    return ReplDirective.CONTINUE


def cmd_expand(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Reveal the full output of a previously-collapsed tool call.

    Usage:
      /expand <tool_use_id>   — print the stored payload for that id (prefix match).
      /expand last            — print the most recent tool call's payload.
      /expand --list          — list every captured tool_use_id in this session.

    The buffer is populated by `repl_ui.render_tool_ok` / `render_tool_error`
    while a REPL session is active. It's session-scoped — a fresh REPL
    starts empty, and `/quit` discards it.
    """
    buf = session.tool_output_buffer
    if not argv:
        console.print(
            "[yellow]Usage:[/yellow] /expand <id> | /expand last | /expand --list"
        )
        return ReplDirective.CONTINUE

    if argv[0] in ("--list", "-l", "list"):
        if not buf:
            console.print("[dim]No tool calls captured yet in this session.[/dim]")
            return ReplDirective.CONTINUE
        table = Table(
            title="Captured tool calls",
            header_style="bold cyan",
        )
        table.add_column("tool_use_id", style="bold")
        table.add_column("tool")
        table.add_column("chars", justify="right")
        table.add_column("status")
        for tid, entry in buf.items():
            short = tid if len(tid) <= 16 else tid[:12] + "…"
            status = "[red]error[/red]" if entry.get("is_error") else "[green]ok[/green]"
            table.add_row(
                short,
                entry.get("tool_name", "?"),
                str(len(entry.get("payload", ""))),
                status,
            )
        console.print(table)
        return ReplDirective.CONTINUE

    if argv[0] == "last":
        if not buf:
            console.print("[dim]No tool calls captured yet in this session.[/dim]")
            return ReplDirective.CONTINUE
        # Insertion-ordered dict — last key is the most recent capture.
        target_id = next(reversed(buf))
        _print_expanded(target_id, buf[target_id])
        return ReplDirective.CONTINUE

    # Prefix match — tool_use_ids can be long (Anthropic style), so allow
    # the user to type just the first ~8 chars when unambiguous.
    needle = argv[0]
    matches = [tid for tid in buf if tid.startswith(needle)]
    if not matches:
        console.print(
            f"[red]No tool call matching id[/red] [bold]{needle}[/bold]. "
            f"Try [cyan]/expand --list[/cyan]."
        )
        return ReplDirective.CONTINUE
    if len(matches) > 1:
        console.print(
            f"[yellow]Ambiguous prefix[/yellow] [bold]{needle}[/bold] matches "
            f"{len(matches)} tool calls. Be more specific:"
        )
        for tid in matches:
            console.print(f"  [cyan]{tid}[/cyan]  ({buf[tid].get('tool_name', '?')})")
        return ReplDirective.CONTINUE

    target_id = matches[0]
    _print_expanded(target_id, buf[target_id])
    return ReplDirective.CONTINUE


def _print_expanded(tool_use_id: str, entry: dict) -> None:
    """Render a single buffered tool result as a bordered block.

    Keeps a visual frame so the expanded output is clearly demarcated from
    the surrounding scrollback and from the next prompt.
    """
    tool_name = entry.get("tool_name", "?")
    payload = entry.get("payload", "")
    is_error = entry.get("is_error", False)
    border = "red" if is_error else "cyan"
    label = "error" if is_error else "output"
    console.print(
        f"\n[bold {border}]── /expand {tool_use_id}  "
        f"{tool_name} {label} ({len(payload)} chars) ──[/bold {border}]"
    )
    # Print the raw payload as-is — no rich markup interpretation so file
    # bodies / JSON can't smuggle markup that breaks the layout.
    console.print(payload, markup=False, highlight=False)
    console.print(f"[bold {border}]── end /expand ──[/bold {border}]\n")


def cmd_verbose(session: ReplSession, argv: list[str]) -> ReplDirective:
    """Toggle DEBUG-level logging. /verbose on|off|toggle (default toggle)."""
    root = logging.getLogger()
    if not argv or argv[0] in ("toggle", ""):
        target = (
            logging.INFO if root.level == logging.DEBUG else logging.DEBUG
        )
    elif argv[0] in ("on", "true", "1"):
        target = logging.DEBUG
    elif argv[0] in ("off", "false", "0"):
        target = logging.INFO
    else:
        console.print("[yellow]Usage:[/yellow] /verbose on|off|toggle")
        return ReplDirective.CONTINUE
    root.setLevel(target)
    console.print(f"log level: [cyan]{logging.getLevelName(target)}[/cyan]")
    return ReplDirective.CONTINUE


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DEFINITIONS: list[SlashCommand] = [
    SlashCommand(
        name="/help",
        summary="List every slash command and what it does.",
        handler=cmd_help,
    ),
    SlashCommand(
        name="/quit",
        summary="Leave the REPL. Triggers the extract-memories nudge.",
        handler=cmd_quit,
    ),
    SlashCommand(
        name="/exit",
        summary="Alias for /quit.",
        handler=cmd_quit,
    ),
    SlashCommand(
        name="/clear",
        summary="Clear the screen. Memory + task state are preserved.",
        handler=cmd_clear,
    ),
    SlashCommand(
        name="/status",
        summary="Show org / provider / current task / memory count / index freshness.",
        handler=cmd_status,
    ),
    SlashCommand(
        name="/index",
        summary="Run build_metadata_index --delta + embed components + embed knowledge base.",
        handler=cmd_index,
    ),
    SlashCommand(
        name="/resume",
        summary="Pick up a persisted task. /resume <id> | --list | --latest. No args -> --list.",
        handler=cmd_resume,
    ),
    SlashCommand(
        name="/tasks",
        summary="Show recent tasks in scope. Pass --all to include completed/failed runs.",
        handler=cmd_tasks,
    ),
    SlashCommand(
        name="/memory",
        summary="Memory tier. /memory <recall|list|extract|export|promote> [...]",
        handler=cmd_memory,
    ),
    SlashCommand(
        name="/mock",
        summary="Toggle mock-org mode. /mock on|off|toggle.",
        handler=cmd_mock,
    ),
    SlashCommand(
        name="/provider",
        summary="Switch LLM provider mid-session. /provider <name> [model].",
        handler=cmd_provider,
    ),
    SlashCommand(
        name="/verbose",
        summary="Toggle DEBUG-level logging. /verbose on|off|toggle.",
        handler=cmd_verbose,
    ),
    SlashCommand(
        name="/mode",
        summary="Show or set operating mode. /mode | /mode plan|execution|general.",
        handler=cmd_mode,
    ),
    SlashCommand(
        name="/expand",
        summary="Reveal a collapsed tool output. /expand <id> | last | --list.",
        handler=cmd_expand,
    ),
]


SLASH_COMMANDS = {cmd.name: cmd for cmd in _DEFINITIONS}


__all__ = [
    "SLASH_COMMANDS",
    "ReplDirective",
    "SlashCommand",
    "SlashHandler",
]
