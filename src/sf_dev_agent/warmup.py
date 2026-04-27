"""Layer A of the auto-warm story (Phase B.2).

When `sf-agent` starts against an org that's never had its context engine
built, soft-prompt the user to warm it up now: build the metadata index,
embed components, embed the bundled knowledge base. Strict opt-in — the
prompt is `[yes / skip / no-and-stop-asking]` and it never auto-runs.

Layer B (the freshness-line injection into the agent's system prompt and
the `check_index_freshness` tool) lives in `index_freshness.py`. This
module is purely the prompt + runner.

Skipped automatically:
    - mock-org mode (warmup requires a live org via sf CLI).
    - When the user previously chose "no-and-stop-asking" for this org
      (sentinel file at `.cache/.skip_warmup_<org>`).

Public API:
    prompt_warmup_if_needed(org, db_path=None, mock_org=False) -> bool
        Returns True iff a warmup actually ran.
    run_warmup(org, db_path=None) -> WarmupResult
        Runs the build/embed sequence with a rich progress display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt

from sf_dev_agent.index_freshness import (
    check_freshness,
    is_warmup_skipped,
    mark_warmup_skipped,
)
from sf_dev_agent.models.schemas import OrgConnection

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class WarmupResult:
    """Outcome of a warmup run — surfaced to the user + logs."""
    success: bool
    components_indexed: int = 0
    components_embedded: int = 0
    knowledge_embedded: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Soft prompt
# ---------------------------------------------------------------------------

def prompt_warmup_if_needed(
    org: OrgConnection,
    db_path: Path | str | None = None,
    mock_org: bool = False,
) -> bool:
    """Prompt the user to warm the engine if this org has never been built.

    Returns True iff a warmup actually ran. Returns False on mock_org,
    skip-flag, "skip" answer, or warmup that errored before completion.
    """
    if mock_org:
        return False

    if db_path is None:
        from sf_dev_agent.context import default_db_path
        db_path = default_db_path()
    db_path = Path(db_path)

    if is_warmup_skipped(db_path, org.org_alias):
        logger.info(
            "Warmup prompt suppressed for %s (skip flag present)",
            org.org_alias,
        )
        return False

    freshness = check_freshness(db_path, org.org_alias)
    if freshness.last_built_at is not None:
        # Already built at least once — the freshness line in the system
        # prompt nudges the agent if it's stale; we don't ask the user.
        return False

    console.print(Panel(
        f"[bold]Context engine not built for [cyan]{org.org_alias}[/cyan][/bold]\n\n"
        "The agent's context engine needs a one-time warm-up: build the "
        "metadata index, embed components for semantic search, embed the "
        "bundled knowledge base. Takes ~30–90 seconds depending on org "
        "size + LLM provider.\n\n"
        "[dim]This is a soft prompt — pick 'no-and-stop-asking' to suppress "
        "it for this org permanently.[/dim]",
        title="[bold]First-run warm-up",
        border_style="cyan",
    ))

    choice = Prompt.ask(
        "Warm up now?",
        choices=["yes", "skip", "no-and-stop-asking"],
        default="yes",
    )

    if choice == "skip":
        console.print(
            "[dim]Skipping warmup. Retrieval layers will return empty "
            "until you call build_metadata_index manually.[/dim]"
        )
        return False

    if choice == "no-and-stop-asking":
        mark_warmup_skipped(db_path, org.org_alias)
        console.print(
            f"[dim]Suppressed for {org.org_alias}. Delete "
            f"`.cache/.skip_warmup_{org.org_alias}` to re-enable.[/dim]"
        )
        return False

    result = run_warmup(org, db_path=db_path)
    if not result.success:
        console.print(
            "[bold yellow]Warmup completed with errors.[/bold yellow] "
            "The agent will still run; some retrieval layers may be empty."
        )
    return result.success


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_warmup(
    org: OrgConnection,
    db_path: Path | str | None = None,
) -> WarmupResult:
    """Execute the build_index → embed_index → embed_knowledge sequence.

    Each step is wrapped in its own progress bar; failures in one step
    don't block the others (e.g., if Gemini quota is hit on
    embed_index, knowledge embedding still runs).
    """
    if db_path is None:
        from sf_dev_agent.context import default_db_path
        db_path = default_db_path()
    db_path = Path(db_path)

    # Lazy imports — keep `warmup` import-cheap so __main__ startup is fast.
    from sf_dev_agent.context import (
        build_index,
        create_embedder,
        embed_index,
        embed_knowledge,
    )

    result = WarmupResult(success=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        # 1. Metadata index ---------------------------------------------
        idx_task = progress.add_task(
            "[cyan]Building metadata index", total=None,
        )
        try:
            build_result = build_index(
                org_alias=org.org_alias,
                db_path=db_path,
                delta=True,  # delta is the safe default (no-op for empty index)
            )
            if not build_result.success:
                msg = (
                    f"build_index failed: {build_result.retrieve_error}"
                    if build_result.retrieve_error else "build_index failed"
                )
                result.errors.append(msg)
                result.success = False
                progress.update(idx_task, description="[red]Index: failed")
            else:
                result.components_indexed = build_result.components_indexed
                progress.update(
                    idx_task,
                    description=(
                        f"[green]Index: {build_result.components_indexed} "
                        f"components"
                    ),
                )
        except Exception as exc:
            result.errors.append(f"build_index raised: {type(exc).__name__}: {exc}")
            result.success = False
            progress.update(idx_task, description="[red]Index: error")
        progress.update(idx_task, completed=100, total=100)

        # 2. Embeddings for components ----------------------------------
        emb_task = progress.add_task(
            "[cyan]Embedding components", total=None,
        )
        try:
            embedder = create_embedder()
            emb_result = embed_index(db_path=db_path, embedder=embedder)
            result.components_embedded = emb_result.embedded
            if emb_result.errors:
                result.errors.extend(emb_result.errors)
            progress.update(
                emb_task,
                description=(
                    f"[green]Components embedded: {emb_result.embedded} "
                    f"(skipped {emb_result.skipped_unchanged})"
                ),
            )
        except (ValueError, ImportError) as exc:
            result.errors.append(
                f"embed_index skipped: {type(exc).__name__}: {exc}"
            )
            progress.update(
                emb_task,
                description=(
                    "[yellow]Components: skipped (no embedder available)"
                ),
            )
        except Exception as exc:
            result.errors.append(f"embed_index raised: {type(exc).__name__}: {exc}")
            progress.update(emb_task, description="[red]Components: error")
        progress.update(emb_task, completed=100, total=100)

        # 3. Embeddings for the bundled knowledge base ------------------
        kb_task = progress.add_task(
            "[cyan]Embedding knowledge base", total=None,
        )
        try:
            embedder = create_embedder()
            kb_result = embed_knowledge(db_path=db_path, embedder=embedder)
            result.knowledge_embedded = kb_result.embedded
            if kb_result.errors:
                result.errors.extend(kb_result.errors)
            progress.update(
                kb_task,
                description=(
                    f"[green]Knowledge embedded: {kb_result.embedded} "
                    f"(skipped {kb_result.skipped_unchanged})"
                ),
            )
        except (ValueError, ImportError) as exc:
            result.errors.append(
                f"embed_knowledge skipped: {type(exc).__name__}: {exc}"
            )
            progress.update(
                kb_task,
                description=(
                    "[yellow]Knowledge: skipped (no embedder available)"
                ),
            )
        except Exception as exc:
            result.errors.append(
                f"embed_knowledge raised: {type(exc).__name__}: {exc}"
            )
            progress.update(kb_task, description="[red]Knowledge: error")
        progress.update(kb_task, completed=100, total=100)

    if result.errors:
        for err in result.errors:
            console.print(f"  [yellow]warning:[/yellow] {err}")

    if result.success and not any(
        msg.startswith(("build_index", "embed_index"))
        for msg in result.errors
    ):
        console.print(
            f"[green]Warm-up complete:[/green] "
            f"{result.components_indexed} components indexed, "
            f"{result.components_embedded} embedded, "
            f"{result.knowledge_embedded} knowledge entries embedded."
        )
    return result
