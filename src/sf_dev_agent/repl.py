"""Persistent terminal REPL — Phase C.1.

Entry-point semantics mirror Claude Code's `claude` binary:

    $ sf-agent          # one word in the OS shell, no args
    ❯ <type freely>     # free-form input goes to agent.run()
    ❯ /help             # slash commands manage session/state
    ❯ /quit             # leave (with extract-nudge in C.5)

`prompt_toolkit` provides the input line: history (persisted to
`~/.sf-agent/history`), tab completion on slash commands, multiline via
backslash continuation, and a status line at the bottom of the
terminal.

The dispatcher (`ReplSession._dispatch`) is intentionally split from
the prompt loop so it's directly testable — tests inject canned inputs
without ever touching prompt_toolkit's interactive layer.

Public API:
    launch_repl(org, provider, mock_org=False, working_memory=None) -> int
    ReplSession                — the running-state object
    format_status_dict(session) -> dict[str, str]   # /status helper
"""

from __future__ import annotations

import logging
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel

from sf_dev_agent.agent import AgentLoop
from sf_dev_agent.index_freshness import (
    check_freshness,
    format_age_human,
)
from sf_dev_agent.memory import (
    MemoryScope,
    MemoryStore,
    WorkingMemoryStore,
)
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.providers.base import LLMProvider
from sf_dev_agent.repl_commands import (
    SLASH_COMMANDS,
    ReplDirective,
)

console = Console()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class ReplSession:
    """Mutable state for one REPL session.

    Attributes are intentionally mutable so slash commands can flip
    `mock_org`, swap `provider`, etc. The agent loop reads them at the
    start of every task.
    """

    def __init__(
        self,
        *,
        org: OrgConnection,
        provider: LLMProvider,
        working_memory: WorkingMemoryStore | None = None,
        mock_org: bool = False,
    ) -> None:
        self.org = org
        self.provider = provider
        self.working_memory = working_memory
        self.mock_org = mock_org
        # Track tasks completed during this REPL session — used by C.5
        # for the /quit extract nudge.
        self.completed_task_ids: list[str] = []

    # ------------------------------------------------------------------
    # Dispatcher — testable without prompt_toolkit
    # ------------------------------------------------------------------

    def _dispatch(self, line: str) -> ReplDirective:
        """Route one line of input. Returns CONTINUE or QUIT."""
        line = line.strip()
        if not line:
            return ReplDirective.CONTINUE

        if line.startswith("/"):
            return self._dispatch_slash(line)

        # Free-form text → start a new agent task.
        return self._dispatch_agent(line)

    def _dispatch_slash(self, line: str) -> ReplDirective:
        parts = line.split()
        head, argv = parts[0], parts[1:]
        cmd = SLASH_COMMANDS.get(head)
        if cmd is None:
            console.print(
                f"[red]Unknown command:[/red] {head}. "
                f"Type [cyan]/help[/cyan] for the full list."
            )
            return ReplDirective.CONTINUE
        try:
            return cmd.handler(self, argv)
        except SystemExit:
            # argparse subcommands inside /memory and /resume call
            # sys.exit on bad args. Catch so the REPL keeps running.
            return ReplDirective.CONTINUE
        except Exception:
            logger.exception("Slash command %s raised", head)
            console.print(
                f"[red]Error in {head}.[/red] "
                f"Re-run with [cyan]/verbose on[/cyan] for details."
            )
            return ReplDirective.CONTINUE

    def _dispatch_agent(self, line: str) -> ReplDirective:
        """Run the agent against `line` as a fresh task.

        After the run completes, check `agent.resume_requested` — if the
        LLM called `request_resume(task_id)` mid-run, hand off to
        `AgentLoop.resume(task_id)` so the user lands back in the
        resumed task without typing a second command. This implements
        the C.4 resume-by-intent flow.
        """
        try:
            agent = AgentLoop(
                org=self.org,
                provider=self.provider,
                mock_org=self.mock_org,
                working_memory=self.working_memory,
                streaming=True,
            )
            task = agent.run(line)
            if task is not None:
                self.completed_task_ids.append(task.task_id)

            # Resume hand-off — the agent signaled it wants the REPL to
            # pick up another task. Loop here so a chain of resumes is
            # possible (rare, but cheap to support).
            while agent.resume_requested is not None:
                target_task_id = agent.resume_requested
                console.print(
                    f"\n[cyan]Resuming task {target_task_id} as requested...[/cyan]"
                )
                if self.working_memory is None:
                    console.print(
                        "[red]Cannot resume — REPL has no working memory store.[/red]"
                    )
                    break
                resumed = AgentLoop.resume(
                    task_id=target_task_id,
                    org=self.org,
                    provider=self.provider,
                    working_memory=self.working_memory,
                    mock_org=self.mock_org,
                )
                if resumed is not None:
                    self.completed_task_ids.append(resumed.task_id)
                # `resume()` reuses the AgentLoop machinery but constructs
                # a fresh inner AgentLoop, so its resume_requested flag
                # belongs to that inner instance — this loop's `agent`
                # object's flag is one-shot. Break to avoid a tight loop.
                break
        except KeyboardInterrupt:
            console.print(
                "\n[yellow]Interrupted.[/yellow] Task state up to this "
                "point is persisted; resume with [cyan]/resume --latest[/cyan]."
            )
        except Exception:
            logger.exception("Agent run raised")
            console.print(
                "[red]Agent run failed.[/red] "
                "Re-run with [cyan]/verbose on[/cyan] for details."
            )
        return ReplDirective.CONTINUE


# ---------------------------------------------------------------------------
# Status line helpers
# ---------------------------------------------------------------------------

def format_status_dict(session: ReplSession) -> dict[str, str]:
    """Compose the per-field status used by /status and the bottom toolbar."""
    org = session.org

    # Working-memory size (count of in-flight tasks for the current scope).
    in_flight_count = "—"
    if session.working_memory is not None:
        try:
            scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
            from sf_dev_agent.memory import TERMINAL_STATUSES
            tasks = session.working_memory.list_tasks(scope=scope, limit=50)
            in_flight = [t for t in tasks if t.status not in TERMINAL_STATUSES]
            in_flight_count = str(len(in_flight))
        except Exception:
            logger.exception("status: in-flight count failed")

    # Project-memory count for the current scope.
    mem_count = "—"
    try:
        from sf_dev_agent.context import default_db_path
        with MemoryStore(default_db_path()) as store:
            mem_count = str(sum(store.stats(
                MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias),
            ).values()))
    except Exception:
        logger.exception("status: memory count failed")

    # Index freshness — repurpose the same line the agent's system prompt sees.
    freshness = "—"
    try:
        from sf_dev_agent.context import default_db_path
        f = check_freshness(default_db_path(), org.org_alias)
        if f.last_built_at is None:
            freshness = "not built"
        elif f.is_stale:
            freshness = f"STALE ({format_age_human(f.age_seconds)})"
        else:
            freshness = format_age_human(f.age_seconds)
    except Exception:
        logger.exception("status: freshness failed")

    return {
        "tenant": org.tenant_id,
        "org": f"{org.org_alias} ({org.org_type})",
        "provider": session.provider.__class__.__name__,
        "model": session.provider.model_name,
        "mock_org": "on" if session.mock_org else "off",
        "in-flight tasks": in_flight_count,
        "memories": mem_count,
        "index": freshness,
    }


def _format_bottom_toolbar(session: ReplSession) -> str:
    """One-line status pinned at the bottom of the terminal."""
    s = format_status_dict(session)
    pieces = [
        f"org={s['org']}",
        f"provider={s['provider'].lower().replace('provider', '')}",
        f"mem={s['memories']}",
        f"tasks={s['in-flight tasks']}",
        f"index={s['index']}",
    ]
    if s["mock_org"] == "on":
        pieces.append("[mock]")
    return " | ".join(pieces)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def _history_path() -> Path:
    """Per-user history file, created on first launch."""
    home = Path.home() / ".sf-agent"
    home.mkdir(parents=True, exist_ok=True)
    return home / "history"


def _build_prompt_session(session: ReplSession) -> PromptSession:
    """Wire prompt_toolkit with history + slash-command completion + status line."""
    completer = WordCompleter(
        sorted(SLASH_COMMANDS.keys()),
        ignore_case=True,
        match_middle=False,
        sentence=True,  # only complete when input starts with the trigger
    )
    return PromptSession(
        message="❯ ",
        history=FileHistory(str(_history_path())),
        completer=completer,
        complete_while_typing=False,
        bottom_toolbar=lambda: _format_bottom_toolbar(session),
        multiline=False,
    )


def _print_banner(session: ReplSession) -> None:
    org = session.org
    mock_label = " [bold yellow][MOCK ORG][/bold yellow]" if session.mock_org else ""

    # Solution A: pull engine state into the banner so memory / task /
    # index counts are visible at first paint, not just in the bottom
    # toolbar (which a small-window user can miss).
    s = format_status_dict(session)
    index_str = s["index"]
    if index_str == "not built":
        index_render = "[bold red]not built[/bold red]"
    elif index_str.startswith("STALE"):
        index_render = f"[bold yellow]{index_str}[/bold yellow]"
    elif index_str == "—":
        index_render = "[dim]—[/dim]"
    else:
        index_render = f"[green]{index_str}[/green]"

    console.print(
        Panel(
            f"[bold]Salesforce Developer Agent[/bold]{mock_label}\n"
            f"Org: {org.org_alias} ({org.org_type}) | "
            f"API v{org.api_version}\n"
            f"Provider: {session.provider.__class__.__name__} | "
            f"Model: {session.provider.model_name}\n"
            f"Memory: {s['memories']} | "
            f"Tasks: {s['in-flight tasks']} in-flight | "
            f"Index: {index_render}\n\n"
            "Type freely to send to the agent. "
            "[cyan]/help[/cyan] lists slash commands. "
            "[cyan]/quit[/cyan] to exit.",
            border_style="green",
        )
    )


def _print_alert_if_needed(session: ReplSession) -> None:
    """Solution B — yellow heads-up panel above the banner when something
    actionable is true (index stale or never built, in-flight tasks
    waiting to be resumed). Quiet on healthy sessions.
    """
    issues: list[str] = []

    try:
        from sf_dev_agent.context import default_db_path
        f = check_freshness(default_db_path(), session.org.org_alias)
        if f.last_built_at is None:
            issues.append(
                "[bold]Index not built[/bold] for this org — retrieval "
                "layers will return empty until you accept the warm-up "
                "prompt or call [cyan]build_metadata_index[/cyan]."
            )
        elif f.is_stale:
            issues.append(
                f"[bold]Index is stale[/bold] "
                f"({format_age_human(f.age_seconds)}) — run "
                "[cyan]build_metadata_index[/cyan] for fresh retrieval."
            )
    except Exception:
        logger.exception("alert: freshness probe failed")

    try:
        if session.working_memory is not None:
            from sf_dev_agent.memory import TERMINAL_STATUSES
            scope = MemoryScope(
                tenant_id=session.org.tenant_id,
                org_alias=session.org.org_alias,
            )
            tasks = session.working_memory.list_tasks(scope=scope, limit=50)
            in_flight = [t for t in tasks if t.status not in TERMINAL_STATUSES]
            if in_flight:
                preview = ", ".join(t.id[:8] for t in in_flight[:3])
                more = (
                    f" +{len(in_flight) - 3} more"
                    if len(in_flight) > 3 else ""
                )
                issues.append(
                    f"[bold]{len(in_flight)} task(s) in-flight[/bold] "
                    f"({preview}{more}) — [cyan]/resume <id>[/cyan] "
                    "or [cyan]/resume --latest[/cyan] to pick up."
                )
    except Exception:
        logger.exception("alert: in-flight task probe failed")

    if not issues:
        return

    body = "\n".join(f"  • {msg}" for msg in issues)
    console.print(Panel(
        body,
        title="[bold yellow]heads up[/bold yellow]",
        border_style="yellow",
    ))


def launch_repl(
    org: OrgConnection,
    provider: LLMProvider,
    mock_org: bool = False,
    working_memory: WorkingMemoryStore | None = None,
) -> int:
    """Run the persistent REPL until /quit or EOF. Returns an exit code."""
    # Open a per-session WorkingMemoryStore by default — consistent with the
    # one-shot CLI path. Caller can pass their own for tests.
    own_working_memory = working_memory is None
    if working_memory is None:
        from sf_dev_agent.context import default_db_path
        working_memory = WorkingMemoryStore(default_db_path())

    session = ReplSession(
        org=org, provider=provider,
        working_memory=working_memory, mock_org=mock_org,
    )

    _print_alert_if_needed(session)
    _print_banner(session)
    pt_session = _build_prompt_session(session)

    try:
        while True:
            try:
                line = pt_session.prompt()
            except (KeyboardInterrupt, EOFError):
                # Ctrl+C / Ctrl+D leaves the REPL with the same effect as /quit.
                console.print()
                break

            directive = session._dispatch(line)
            if directive == ReplDirective.QUIT:
                break
    finally:
        if own_working_memory:
            try:
                working_memory.close()
            except Exception:
                logger.exception("Failed to close working memory")

    # C.5: extract-nudge — soft prompt to scan completed tasks for
    # save-worthy memories. Honors `[yes / skip / no-and-stop-asking]`
    # and a per-(tenant, org) sentinel; no-ops if there's nothing to
    # extract or working memory is detached.
    if session.completed_task_ids:
        try:
            from sf_dev_agent.extract_nudge import prompt_extract_if_needed
            saved = prompt_extract_if_needed(session)
        except Exception:
            logger.exception("Extract nudge raised — continuing teardown")
            saved = 0

        msg = (
            f"[dim]Session ended. {len(session.completed_task_ids)} task(s) "
            "ran this session"
        )
        if saved:
            msg += f"; {saved} memories saved"
        msg += ".[/dim]"
        console.print(msg)
    else:
        console.print("[dim]Session ended.[/dim]")
    return 0


__all__ = [
    "ReplSession",
    "format_status_dict",
    "launch_repl",
]
