"""CLI verb: `sf-agent resume [<task-id>] [--list | --latest] [...]`.

Phase B.3 of the post-Wave-8 roadmap. The resume *capability* shipped in
Wave 8 slice 2b (`AgentLoop.resume`); this module is purely the OS-shell
plumbing on top of it.

Three usages:
    sf-agent resume <task-id>       Resume that specific task.
    sf-agent resume --list          Show in-flight tasks in scope.
    sf-agent resume --latest        Resume the most-recent in-flight task.

Why this exists alongside the in-REPL natural-language `/resume` (planned
for phase C.4): the two surfaces are complementary, not redundant. The
CLI verb covers scripted / one-off use (CI jobs, desktop shortcuts,
"I crashed last night; pick that up") where dropping into the REPL would
be friction. C.4 covers in-REPL ambient intent recognition.

Both share the underlying `AgentLoop.resume()` and
`WorkingMemoryStore.list_tasks()` machinery.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from rich.console import Console
from rich.table import Table

from sf_dev_agent.agent import AgentLoop
from sf_dev_agent.index_freshness import format_age_human
from sf_dev_agent.memory import (
    TERMINAL_STATUSES,
    MemoryScope,
    TaskRow,
    WorkingMemoryStore,
)
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.providers import PROVIDERS, create_provider
from sf_dev_agent.sf_config import (
    derive_api_version,
    derive_instance_url,
    derive_org_type,
)

console = Console()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_resume_command(argv: list[str]) -> int:
    """Entry point. `argv` is everything after `resume`. Returns exit code."""
    args = _parse_args(argv)

    org_alias = args.org_alias or os.environ.get("SF_ORG_ALIAS", "").strip()
    if not org_alias:
        console.print(
            "[bold red]No Salesforce org configured.[/bold red] "
            "Set SF_ORG_ALIAS in your .env, or pass --org-alias."
        )
        return 1

    org = _build_org(org_alias, args)
    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)

    from sf_dev_agent.context import default_db_path
    db_path = default_db_path()

    if args.list:
        return _cmd_list(scope, db_path)

    return _cmd_resume(args, org, scope, db_path)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_list(scope: MemoryScope, db_path) -> int:
    with WorkingMemoryStore(db_path) as store:
        all_recent = store.list_tasks(scope=scope, limit=50)

    in_flight = [t for t in all_recent if t.status not in TERMINAL_STATUSES]
    if not in_flight:
        console.print(
            "[dim]No in-flight tasks found in this scope.[/dim] "
            "(Pass --include-terminal to see completed/failed runs.)"
        )
        return 0

    console.print(_render_task_table(in_flight, scope))
    return 0


def _cmd_resume(
    args: argparse.Namespace,
    org: OrgConnection,
    scope: MemoryScope,
    db_path,
) -> int:
    # Resolve the target task_id from --latest or the positional argument.
    if args.latest:
        task_id = _resolve_latest(scope, db_path)
        if task_id is None:
            console.print(
                "[bold red]No in-flight tasks to resume.[/bold red] "
                "Use [cyan]sf-agent resume --list[/cyan] to confirm or "
                "start a new task with [cyan]sf-agent[/cyan]."
            )
            return 1
    else:
        task_id = args.task_id

    if not task_id:
        console.print(
            "[bold red]Specify a task-id, --latest, or --list.[/bold red] "
            "Run [cyan]sf-agent resume --list[/cyan] to see in-flight tasks."
        )
        return 1

    # Build the provider AFTER target resolution — failures here are
    # provider-config issues, not task-not-found issues.
    try:
        provider = create_provider(provider=args.provider, model=args.model)
    except (ImportError, ValueError) as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        return 1

    store = WorkingMemoryStore(db_path)
    try:
        AgentLoop.resume(
            task_id=task_id,
            org=org,
            provider=provider,
            working_memory=store,
            mock_org=args.mock_org,
        )
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 1
    finally:
        store.close()

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_latest(scope: MemoryScope, db_path) -> str | None:
    """Return the task_id of the most-recent in-flight task in scope."""
    with WorkingMemoryStore(db_path) as store:
        rows = store.list_tasks(scope=scope, limit=50)
    in_flight = [r for r in rows if r.status not in TERMINAL_STATUSES]
    if not in_flight:
        return None
    # list_tasks returns newest-first by created_at.
    return in_flight[0].id


def _render_task_table(rows: list[TaskRow], scope: MemoryScope) -> Table:
    title = f"In-flight tasks for tenant={scope.tenant_id}"
    if scope.org_alias:
        title += f", org={scope.org_alias}"

    table = Table(title=title, header_style="bold cyan")
    table.add_column("Task ID", style="bold")
    table.add_column("Status")
    table.add_column("Plan?")
    table.add_column("Description", overflow="fold")
    table.add_column("Created", style="dim")

    from datetime import UTC, datetime
    now = datetime.now(UTC)

    for r in rows:
        try:
            ts = datetime.fromisoformat(r.created_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_phrase = format_age_human((now - ts).total_seconds())
        except ValueError:
            age_phrase = r.created_at

        plan_marker = (
            "[green]approved[/green]" if r.plan_approved
            else ("[yellow]drafted[/yellow]" if r.plan_json else "[dim]none[/dim]")
        )

        # Truncate user_request to ~80 chars for the description column.
        desc = r.user_request.strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:77] + "..."

        table.add_row(r.id, r.status, plan_marker, desc, age_phrase)
    return table


def _build_org(org_alias: str, args: argparse.Namespace) -> OrgConnection:
    org_type = (
        args.org_type
        or os.environ.get("SF_ORG_TYPE")
        or derive_org_type(org_alias)
    )
    instance_url = (
        args.instance_url
        or os.environ.get("SF_INSTANCE_URL")
        or derive_instance_url(org_alias)
    )
    api_version = (
        args.api_version
        or os.environ.get("SF_API_VERSION")
        or derive_api_version()
    )
    return OrgConnection(
        tenant_id="local-dev",
        org_alias=org_alias,
        org_type=org_type,
        instance_url=instance_url,
        api_version=api_version,
    )


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sf-agent resume",
        description=(
            "Resume a persisted task. With a task-id, resumes that task. "
            "With --latest, resumes the most-recent in-flight task. With "
            "--list, prints in-flight tasks without resuming anything."
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="The task to resume. Omit when using --list or --latest.",
    )
    target.add_argument(
        "--list",
        action="store_true",
        help="List in-flight tasks in scope; do not resume anything.",
    )
    target.add_argument(
        "--latest",
        action="store_true",
        help="Resume the most-recent in-flight task in scope.",
    )

    parser.add_argument(
        "--provider",
        choices=list(PROVIDERS),
        default=None,
        help="LLM provider (default: env-resolved).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model override (default: provider default).",
    )
    parser.add_argument(
        "--org-alias",
        default=None,
        help="Override the env's SF_ORG_ALIAS for this resume.",
    )
    parser.add_argument(
        "--org-type",
        choices=["sandbox", "scratch", "production", "developer"],
        default=None,
    )
    parser.add_argument("--instance-url", default=None)
    parser.add_argument("--api-version", default=None)
    parser.add_argument(
        "--mock-org",
        action="store_true",
        help="Stub all sf CLI calls (mirrors `sf-agent --mock-org`).",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Direct entry — used by the __main__.py special-case dispatch."""
    sys.exit(run_resume_command(sys.argv[2:]))
