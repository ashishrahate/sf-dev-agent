"""CLI subcommands for the memory tier — `sf-agent memory <verb>`.

Three verbs:
    extract  — scan a persisted task transcript for save-worthy moments,
               present each candidate to the user, persist the accepted ones.
    export   — dump memories to Markdown for git versioning + transparency.
    promote  — draft a knowledge_base entry from a project memory.

Wired into `__main__.py` via a special-case dispatch (mirrors `setup`),
not argparse subparsers — the existing top-level parser uses a positional
`request`, and adding subparsers would change that contract.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from sf_dev_agent.memory import (
    MemoryScope,
    MemoryStore,
    WorkingMemoryStore,
)
from sf_dev_agent.memory.export import MemoryExporter, default_export_dir
from sf_dev_agent.memory.extraction import (
    ExtractedMemoryCandidate,
    MemoryExtractor,
)
from sf_dev_agent.memory.promote import KNOWLEDGE_CATEGORIES, MemoryPromoter
from sf_dev_agent.providers import create_provider

console = Console()


def run_memory_command(argv: list[str]) -> int:
    """Entry point. `argv` is everything after `memory`.

    Returns the process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="sf-agent memory",
        description="Memory-tier maintenance commands",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    # extract -----------------------------------------------------------
    p_extract = sub.add_parser(
        "extract",
        help="Run end-of-session extraction against a persisted task transcript",
    )
    p_extract.add_argument("--task-id", required=True, help="The task to extract from.")
    p_extract.add_argument(
        "--provider", default=None,
        help="LLM provider (default: env-resolved).",
    )
    p_extract.add_argument(
        "--model", default=None,
        help="Model override (default: provider default).",
    )
    p_extract.add_argument(
        "--min-confidence", type=float, default=0.6,
        help="Drop candidates below this confidence (default 0.6).",
    )
    p_extract.add_argument(
        "--max-candidates", type=int, default=5,
        help="Cap candidates surfaced (default 5).",
    )
    p_extract.add_argument(
        "--db-path", default=None,
        help="SQLite DB path (default: package default index location).",
    )

    # export ------------------------------------------------------------
    p_export = sub.add_parser(
        "export",
        help="Dump memories to Markdown for git versioning",
    )
    p_export.add_argument(
        "--type", choices=["user", "feedback", "project", "reference"],
        default=None, help="Restrict to one memory type.",
    )
    p_export.add_argument(
        "--out", default=None,
        help=f"Output dir (default: {default_export_dir()}).",
    )
    p_export.add_argument(
        "--cross-org", action="store_true",
        help="Export the cross-org (org_alias=NULL) tier instead of the current org.",
    )
    p_export.add_argument(
        "--include-superseded", action="store_true",
        help="Include memories that were merged away by compaction.",
    )
    p_export.add_argument(
        "--db-path", default=None,
        help="SQLite DB path (default: package default index location).",
    )

    # promote -----------------------------------------------------------
    p_promote = sub.add_parser(
        "promote",
        help="Draft a knowledge_base entry from a project memory",
    )
    p_promote.add_argument("--memory-id", required=True)
    p_promote.add_argument(
        "--category", required=True, choices=sorted(KNOWLEDGE_CATEGORIES),
    )
    p_promote.add_argument(
        "--severity",
        choices=["critical", "high", "medium", "low", "info"],
        default=None,
    )
    p_promote.add_argument(
        "--title", default=None,
        help="Override the entry title (default: the memory's description).",
    )
    p_promote.add_argument(
        "--tags", default=None,
        help="Comma-separated tags (default: copy from the memory).",
    )
    p_promote.add_argument(
        "--reference", action="append", default=[],
        help="URL to attach as a reference. Repeat for multiple.",
    )
    p_promote.add_argument(
        "--force", action="store_true",
        help="Promote even if heuristics flag tenant-specific content.",
    )
    p_promote.add_argument(
        "--out-dir", default=None,
        help="Override entries dir (default: bundled context/knowledge/entries).",
    )
    p_promote.add_argument(
        "--db-path", default=None,
        help="SQLite DB path (default: package default index location).",
    )

    args = parser.parse_args(argv)

    if args.verb == "extract":
        return _cmd_extract(args)
    if args.verb == "export":
        return _cmd_export(args)
    if args.verb == "promote":
        return _cmd_promote(args)
    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def _cmd_extract(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)

    try:
        provider = create_provider(provider=args.provider, model=args.model)
    except (ImportError, ValueError) as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        return 1

    wm = WorkingMemoryStore(db_path)
    extractor = MemoryExtractor(
        working_memory=wm, provider=provider,
        min_confidence=args.min_confidence,
        max_candidates=args.max_candidates,
    )
    try:
        result = extractor.extract(args.task_id)
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        wm.close()
        return 1

    if result.parse_error:
        console.print(
            Panel(
                f"[bold red]Extraction failed[/bold red]\n{result.parse_error}",
                border_style="red",
            )
        )
        if result.raw_response:
            console.print("[dim]Raw model response:[/dim]")
            console.print(result.raw_response[:2000])
        wm.close()
        return 1

    if not result.candidates:
        console.print(
            f"[dim]No save-worthy candidates found for {args.task_id} "
            f"(skipped {result.skipped_low_confidence} below threshold).[/dim]"
        )
        wm.close()
        return 0

    console.print(
        Panel(
            f"task_id: {args.task_id}\n"
            f"candidates: {len(result.candidates)} "
            f"| skipped low-confidence: {result.skipped_low_confidence}",
            title="[bold]Memory extraction", border_style="cyan",
        )
    )

    org_alias = _org_alias_from_env()
    scope = MemoryScope(tenant_id="local-dev", org_alias=org_alias)
    accepted: list[ExtractedMemoryCandidate] = []

    with MemoryStore(db_path) as ms:
        for i, cand in enumerate(result.candidates, start=1):
            _present_candidate(i, len(result.candidates), cand)
            choice = Prompt.ask(
                "Save this memory?",
                choices=["yes", "no", "edit"],
                default="no",
            )
            if choice == "no":
                continue
            if choice == "edit":
                cand = _edit_candidate(cand)
                if cand is None:
                    continue
            ms.save(
                scope=scope,
                type=cand.type,
                name=cand.name,
                description=cand.description,
                body=cand.body,
                source_session_id=args.task_id,
            )
            accepted.append(cand)

    wm.close()

    console.print(
        f"[green]Saved {len(accepted)} memories.[/green] "
        "Run [cyan]embed_memories[/cyan] before recall to embed the new rows."
    )
    return 0


def _present_candidate(
    index: int, total: int, cand: ExtractedMemoryCandidate,
) -> None:
    md = (
        f"### Candidate {index} of {total}\n\n"
        f"- **type**: `{cand.type}`\n"
        f"- **name**: `{cand.name}`\n"
        f"- **confidence**: {cand.confidence:.2f}\n\n"
        f"**Description**\n\n{cand.description}\n\n"
        f"**Body**\n\n{cand.body}\n\n"
        f"**Evidence (transcript quote)**\n\n> {cand.evidence_quote or '(none provided)'}"
    )
    console.print(Panel(Markdown(md), border_style="yellow"))


def _edit_candidate(
    cand: ExtractedMemoryCandidate,
) -> ExtractedMemoryCandidate | None:
    """Inline edit pass — let the user override name/description/body before save."""
    console.print("[dim]Edit fields (blank to keep current value).[/dim]")
    name = Prompt.ask("name", default=cand.name)
    description = Prompt.ask("description", default=cand.description)
    body = Prompt.ask("body", default=cand.body)
    if not name.strip() or not description.strip() or not body.strip():
        console.print("[red]Empty field — skipping this candidate.[/red]")
        return None
    return ExtractedMemoryCandidate(
        type=cand.type,
        name=name.strip(),
        description=description.strip(),
        body=body.strip(),
        confidence=cand.confidence,
        evidence_quote=cand.evidence_quote,
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _cmd_export(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    out_dir = Path(args.out) if args.out else None

    org_alias = None if args.cross_org else _org_alias_from_env()
    scope = MemoryScope(tenant_id="local-dev", org_alias=org_alias)

    with MemoryStore(db_path) as ms:
        exporter = MemoryExporter(store=ms, out_dir=out_dir)
        result = exporter.export(
            scope=scope,
            type=args.type,
            include_superseded=args.include_superseded,
        )

    if not result.files:
        console.print("[dim]No memories matched — nothing exported.[/dim]")
        return 0

    console.print(
        f"[green]Exported {len(result.files)} memories[/green] to "
        f"[cyan]{exporter.out_dir}[/cyan]"
    )
    if result.skipped:
        console.print(f"[yellow]Skipped {result.skipped} due to write errors.[/yellow]")
    return 0


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------

def _cmd_promote(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    tags = _parse_tags(args.tags)

    with MemoryStore(db_path) as ms:
        promoter = MemoryPromoter(store=ms, entries_dir=args.out_dir)
        try:
            result = promoter.promote(
                memory_id=args.memory_id,
                category=args.category,
                severity=args.severity,
                tags=tags,
                title=args.title,
                references=list(args.reference),
                force=args.force,
            )
        except ValueError as exc:
            console.print(f"[bold red]{exc}[/bold red]")
            return 1
        except FileExistsError as exc:
            console.print(f"[bold red]{exc}[/bold red]")
            return 1

    if result.skipped:
        console.print(
            Panel(
                "[bold yellow]Tenant-specific content detected — refusing "
                "to write the draft.[/bold yellow]\n\n"
                + "\n".join(f"  - {w}" for w in result.warnings)
                + "\n\nRe-run with [cyan]--force[/cyan] once you've reviewed "
                "and rephrased to generic platform knowledge.",
                border_style="yellow",
            )
        )
        return 1

    console.print(f"[green]Wrote draft knowledge entry[/green] to [cyan]{result.file}[/cyan]")
    if result.warnings:
        console.print(
            Panel(
                "[yellow]Heuristics flagged the following — verify before "
                "committing:[/yellow]\n\n"
                + "\n".join(f"  - {w}" for w in result.warnings),
                border_style="yellow",
            )
        )
    console.print(
        "Review the draft, edit if needed, then "
        "[cyan]git add[/cyan] + commit it."
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(arg_path: str | None) -> Path:
    if arg_path:
        return Path(arg_path)
    from sf_dev_agent.context import default_db_path
    return default_db_path()


def _org_alias_from_env() -> str | None:
    """The CLI only knows org_alias from the env (no live OrgConnection here)."""
    alias = os.environ.get("SF_ORG_ALIAS", "").strip()
    return alias or None


def _parse_tags(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [t.strip() for t in raw.split(",") if t.strip()]


def main() -> None:
    """Entry point for the dispatched-from-__main__ flow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.exit(run_memory_command(sys.argv[2:]))
