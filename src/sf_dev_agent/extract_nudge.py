"""End-of-session memory-extraction nudge (Phase C.5).

When the user types `/quit` after running tasks during a REPL session,
soft-prompt them to scan those tasks for save-worthy moments and
optionally save the candidates as project memories.

Strict opt-in — `[yes / skip / no-and-stop-asking]`. Same shape as the
B.2 warmup nudge so the UX stays consistent across the two soft prompts.

Skipped automatically when:
    - There are no completed tasks this session.
    - There's no `WorkingMemoryStore` attached.
    - The `.skip_extract_<scope>` sentinel exists for this scope.

Public API:
    prompt_extract_if_needed(session) -> int
        Returns the number of memories saved (0 on any skip / no-op).
    extract_skip_path(db_path, tenant_id, org_alias) -> Path
    is_extract_skipped(db_path, tenant_id, org_alias) -> bool
    mark_extract_skipped(db_path, tenant_id, org_alias) -> None
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from sf_dev_agent.memory import MemoryScope, MemoryStore
from sf_dev_agent.memory.extraction import (
    ExtractedMemoryCandidate,
    MemoryExtractor,
)

if TYPE_CHECKING:
    from sf_dev_agent.repl import ReplSession

logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Skip-sentinel
# ---------------------------------------------------------------------------

def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def extract_skip_path(
    db_path: Path | str, tenant_id: str, org_alias: str,
) -> Path:
    """Per-(tenant, org) sentinel that suppresses the extract nudge."""
    db_path = Path(db_path)
    cache_dir = db_path.parent
    return cache_dir / f".skip_extract_{_safe(tenant_id)}_{_safe(org_alias)}"


def is_extract_skipped(
    db_path: Path | str, tenant_id: str, org_alias: str,
) -> bool:
    return extract_skip_path(db_path, tenant_id, org_alias).exists()


def mark_extract_skipped(
    db_path: Path | str, tenant_id: str, org_alias: str,
) -> None:
    path = extract_skip_path(db_path, tenant_id, org_alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"extract-nudge suppressed for tenant={tenant_id} org={org_alias}\n"
        f"created at {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        "delete this file to re-enable the prompt\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Soft prompt + runner
# ---------------------------------------------------------------------------

def prompt_extract_if_needed(session: ReplSession) -> int:
    """Prompt the user to extract memories from this session's completed tasks.

    Returns the number of memories actually saved (0 when there's nothing
    to do, the user skipped, suppressed, or extraction produced nothing).
    """
    task_ids = list(session.completed_task_ids)
    if not task_ids:
        return 0

    if session.working_memory is None:
        logger.info("Extract nudge skipped — no WorkingMemoryStore on session")
        return 0

    from sf_dev_agent.context import default_db_path
    db_path = default_db_path()

    if is_extract_skipped(db_path, session.org.tenant_id, session.org.org_alias):
        logger.info(
            "Extract nudge suppressed for tenant=%s org=%s (skip flag present)",
            session.org.tenant_id, session.org.org_alias,
        )
        return 0

    plural = "s" if len(task_ids) != 1 else ""
    console.print(Panel(
        f"[bold]{len(task_ids)} task{plural} completed this session[/bold]\n\n"
        "Run end-of-session memory extraction now? The agent will scan each "
        "transcript for durable facts (user preferences, project decisions, "
        "validated approaches) and propose memory candidates. You confirm "
        "each one before it's saved.\n\n"
        "[dim]Soft prompt — pick 'no-and-stop-asking' to suppress for this "
        "(tenant, org) permanently. You can always run "
        "[cyan]sf-agent memory extract --task-id <id>[/cyan] later.[/dim]",
        title="[bold]Memory extraction",
        border_style="cyan",
    ))

    choice = Prompt.ask(
        "Extract now?",
        choices=["yes", "skip", "no-and-stop-asking"],
        default="skip",
    )

    if choice == "skip":
        return 0

    if choice == "no-and-stop-asking":
        mark_extract_skipped(
            db_path, session.org.tenant_id, session.org.org_alias,
        )
        console.print(
            f"[dim]Suppressed for tenant={session.org.tenant_id} "
            f"org={session.org.org_alias}. Delete the sentinel file to re-enable.[/dim]"
        )
        return 0

    return _run_extraction(session, db_path, task_ids)


def _run_extraction(
    session: ReplSession,
    db_path: Path,
    task_ids: list[str],
) -> int:
    """Run MemoryExtractor for each task and present candidates inline."""
    extractor = MemoryExtractor(
        working_memory=session.working_memory,
        provider=session.provider,
    )
    scope = MemoryScope(
        tenant_id=session.org.tenant_id, org_alias=session.org.org_alias,
    )

    saved_total = 0

    with MemoryStore(db_path) as ms:
        for task_id in task_ids:
            console.print(f"\n[cyan]Extracting from {task_id}...[/cyan]")
            try:
                result = extractor.extract(task_id)
            except ValueError as exc:
                console.print(f"  [yellow]skipped:[/yellow] {exc}")
                continue

            if result.parse_error:
                console.print(
                    f"  [yellow]extraction errored:[/yellow] {result.parse_error}"
                )
                continue

            if not result.candidates:
                console.print(
                    f"  [dim]no candidates (skipped "
                    f"{result.skipped_low_confidence} below threshold)[/dim]"
                )
                continue

            saved_total += _present_and_save(ms, scope, task_id, result.candidates)

    if saved_total:
        console.print(
            f"\n[green]Saved {saved_total} memories.[/green] "
            "Run [cyan]embed_memories[/cyan] before recall to embed them."
        )
    return saved_total


def _present_and_save(
    ms: MemoryStore,
    scope: MemoryScope,
    task_id: str,
    candidates: list[ExtractedMemoryCandidate],
) -> int:
    """Show each candidate and ask yes/no/edit. Returns the number saved."""
    saved = 0
    for i, cand in enumerate(candidates, start=1):
        md = (
            f"### Candidate {i} of {len(candidates)} (from {task_id})\n\n"
            f"- **type**: `{cand.type}`\n"
            f"- **name**: `{cand.name}`\n"
            f"- **confidence**: {cand.confidence:.2f}\n\n"
            f"**Description**\n\n{cand.description}\n\n"
            f"**Body**\n\n{cand.body}\n\n"
            f"**Evidence**\n\n> {cand.evidence_quote or '(none provided)'}"
        )
        console.print(Panel(Markdown(md), border_style="yellow"))

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
            source_session_id=task_id,
        )
        saved += 1

    return saved


def _edit_candidate(
    cand: ExtractedMemoryCandidate,
) -> ExtractedMemoryCandidate | None:
    """Inline edit pass — same UX as the memory-CLI extract path."""
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


__all__ = [
    "extract_skip_path",
    "is_extract_skipped",
    "mark_extract_skipped",
    "prompt_extract_if_needed",
]
