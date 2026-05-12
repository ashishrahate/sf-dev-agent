"""CLI subcommand for the LLM token audit — `sf-agent audit tokens`.

Renders the three aggregation views surfaced by `LLMAuditStore`:

    sf-agent audit tokens --by tool [--since 7d] [--tenant ...] [--org ...]
    sf-agent audit tokens --by model [filters...]
    sf-agent audit tokens --task <id>
    sf-agent audit tokens --summary [filters...]

Default view is `--summary` so a bare `sf-agent audit tokens` prints
totals. Output uses `rich.table` for terminal-friendly columns.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sf_dev_agent.audit import LLMAuditStore
from sf_dev_agent.context import default_db_path

console = Console()


# ---------------------------------------------------------------------------
# Entry point — invoked by __main__.py's special-case dispatch
# ---------------------------------------------------------------------------

def run_audit_command(argv: list[str]) -> int:
    """`argv` is everything after the `audit` word in the parent CLI."""
    parser = argparse.ArgumentParser(
        prog="sf-agent audit",
        description="LLM call audit / token usage reports.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    p_tokens = sub.add_parser(
        "tokens",
        help="Show token usage across LLM calls.",
        description=(
            "Aggregate token usage with optional groupings. Default view is "
            "an overall summary; --by tool / --by model surface where tokens "
            "actually go; --task lists every turn of one task."
        ),
    )
    p_tokens.add_argument(
        "--by", choices=["tool", "model"], default=None,
        help="Aggregate by this grouping (default: summary totals).",
    )
    p_tokens.add_argument(
        "--task", default=None,
        help="Show every turn of one task in order (overrides --by).",
    )
    p_tokens.add_argument(
        "--summary", action="store_true",
        help="Force the summary view even when --by is set.",
    )
    p_tokens.add_argument(
        "--tenant", default=None,
        help="Filter to one tenant (default: all).",
    )
    p_tokens.add_argument(
        "--org", default=None,
        help="Filter to one org alias (default: all).",
    )
    p_tokens.add_argument(
        "--since", default=None,
        help=(
            "Only include rows newer than this. Accepts ISO-8601 or a "
            "shorthand like '7d', '24h', '90m', '30s'."
        ),
    )
    p_tokens.add_argument(
        "--include-untriggered", action="store_true",
        help=(
            "When grouping --by tool, include first-turn rows whose tool "
            "trigger is NULL. Default: include."
        ),
    )
    p_tokens.add_argument(
        "--exclude-untriggered", action="store_true",
        help="Inverse of --include-untriggered for the --by tool view.",
    )
    p_tokens.add_argument(
        "--db-path", default=None,
        help="SQLite DB path (default: package default index location).",
    )

    args = parser.parse_args(argv)
    if args.verb != "tokens":
        parser.error(f"unknown verb: {args.verb}")

    db_path = Path(args.db_path) if args.db_path else default_db_path()
    if not db_path.exists():
        console.print(
            f"[yellow]No audit DB found at[/yellow] {db_path}\n"
            "[dim]Run an agent task first; audit rows are written as the "
            "loop calls the LLM.[/dim]"
        )
        return 0

    since = _resolve_since(args.since) if args.since else None
    include_untriggered = not args.exclude_untriggered

    with LLMAuditStore(db_path) as store:
        if args.task:
            return _render_task(store, args.task)
        if args.summary or args.by is None:
            return _render_summary(
                store, tenant=args.tenant, org=args.org, since=since,
            )
        if args.by == "tool":
            return _render_by_tool(
                store, tenant=args.tenant, org=args.org, since=since,
                include_untriggered=include_untriggered,
            )
        if args.by == "model":
            return _render_by_model(
                store, tenant=args.tenant, org=args.org, since=since,
            )

    return 0


# ---------------------------------------------------------------------------
# View renderers — small enough that each gets its own function
# ---------------------------------------------------------------------------

def _render_summary(
    store: LLMAuditStore, *,
    tenant: str | None, org: str | None, since: str | None,
) -> int:
    summary = store.summary(tenant_id=tenant, org_alias=org, since=since)
    table = Table(
        title="LLM token summary",
        header_style="bold cyan", show_header=False,
    )
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    for key, value in summary.items():
        table.add_row(key, _fmt_number(value))
    console.print(table)
    return 0


def _render_by_tool(
    store: LLMAuditStore, *,
    tenant: str | None, org: str | None, since: str | None,
    include_untriggered: bool,
) -> int:
    rows = store.aggregate_by_tool(
        tenant_id=tenant, org_alias=org, since=since,
        include_untriggered=include_untriggered,
    )
    if not rows:
        console.print("[dim]No LLM calls match the filter.[/dim]")
        return 0
    table = Table(title="Tokens by triggering tool", header_style="bold cyan")
    table.add_column("tool", style="bold")
    table.add_column("calls", justify="right")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("cache_read", justify="right")
    table.add_column("cache_write", justify="right")
    table.add_column("total", justify="right", style="bold")
    total_input = total_output = total_cr = total_cw = total_calls = 0
    for r in rows:
        label = r.tool_name or "[dim](first turn)[/dim]"
        table.add_row(
            label,
            _fmt_number(r.calls),
            _fmt_number(r.input_tokens),
            _fmt_number(r.output_tokens),
            _fmt_number(r.cache_read_tokens),
            _fmt_number(r.cache_write_tokens),
            _fmt_number(r.total_tokens),
        )
        total_calls += r.calls
        total_input += r.input_tokens
        total_output += r.output_tokens
        total_cr += r.cache_read_tokens
        total_cw += r.cache_write_tokens
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        _fmt_number(total_calls),
        _fmt_number(total_input),
        _fmt_number(total_output),
        _fmt_number(total_cr),
        _fmt_number(total_cw),
        _fmt_number(total_input + total_output),
    )
    console.print(table)
    return 0


def _render_by_model(
    store: LLMAuditStore, *,
    tenant: str | None, org: str | None, since: str | None,
) -> int:
    rows = store.aggregate_by_model(
        tenant_id=tenant, org_alias=org, since=since,
    )
    if not rows:
        console.print("[dim]No LLM calls match the filter.[/dim]")
        return 0
    table = Table(title="Tokens by provider + model", header_style="bold cyan")
    table.add_column("provider", style="bold")
    table.add_column("model")
    table.add_column("calls", justify="right")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("cache_read", justify="right")
    for r in rows:
        table.add_row(
            r.provider, r.model,
            _fmt_number(r.calls),
            _fmt_number(r.input_tokens),
            _fmt_number(r.output_tokens),
            _fmt_number(r.cache_read_tokens),
        )
    console.print(table)
    return 0


def _render_task(store: LLMAuditStore, task_id: str) -> int:
    rows = store.list_for_task(task_id)
    if not rows:
        console.print(
            f"[yellow]No LLM calls recorded for task[/yellow] [bold]{task_id}[/bold]"
        )
        return 0
    table = Table(
        title=f"LLM calls for task {task_id}", header_style="bold cyan",
    )
    table.add_column("turn", justify="right")
    table.add_column("model")
    table.add_column("triggered_by")
    table.add_column("emitted")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("ms", justify="right")
    for r in rows:
        emitted = ", ".join(r.emitted_tools) if r.emitted_tools else "—"
        table.add_row(
            str(r.turn_idx),
            f"{r.provider.replace('Provider', '').lower()}/{r.model}",
            r.triggered_by_tool or "[dim]—[/dim]",
            emitted,
            _fmt_number(r.usage.input_tokens),
            _fmt_number(r.usage.output_tokens),
            _fmt_number(r.duration_ms),
        )
    console.print(table)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHORTHAND_PATTERN = re.compile(r"^(\d+)\s*([smhd])$")


def _resolve_since(value: str) -> str:
    """Accept ISO-8601 or shorthand (`7d`, `24h`, `90m`, `30s`) and return
    an ISO-8601 lower bound suitable for SQL comparison.

    On unparseable input, we fall through to returning the original value
    so the user gets the natural empty-result behavior rather than a
    silent filter mismatch.
    """
    m = _SHORTHAND_PATTERN.match(value.strip())
    if not m:
        return value
    n, unit = int(m.group(1)), m.group(2)
    unit_to_kw = {
        "s": "seconds", "m": "minutes",
        "h": "hours", "d": "days",
    }
    cutoff = datetime.now(UTC) - timedelta(**{unit_to_kw[unit]: n})
    return cutoff.isoformat()


def _fmt_number(n: int) -> str:
    """Thousands separators for readability — large token counts get hard
    to eyeball without them."""
    return f"{n:,}"


__all__ = ["run_audit_command"]


if __name__ == "__main__":  # pragma: no cover - manual entry
    sys.exit(run_audit_command(sys.argv[1:]))
